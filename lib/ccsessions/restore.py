"""`cc-sessions restore`: reopen the sessions a crash, power loss or shutdown killed.

When any tab snapshot exists (tabs.py) restore reads the last tab list of the dead iTerm2
instance, in order: `select_snapshot` and `run_snapshot`. Everything below describes the
legacy path, used only on a machine with no snapshot yet.

Candidates come from the ledger, never from Claude Code's own registry:
  - crash: a row whose process is gone but whose SessionEnd never arrived (ended_at NULL);
  - shutdown cluster: rows that ended with reason 'other' (what a logout/shutdown SIGTERM
    produces) together, right before boot. T = the newest such ended_at before boot, used
    only when T is within 5 min of the newest pre-boot activity; the cluster walks back
    from T while consecutive ended_at values are within 30 s of each other.
A deliberate exit (/exit, Ctrl-D: reason prompt_input_exit, clear, logout...) is never
restored. Hibernated rows are left alone unless --include-hibernated, or --auto finds
their tab gone; those tabs get the resume command typed but not run. A hibernated row
whose tab is still open is never reopened (that would be a second tab for one session).
With --include-hibernated (never under --auto), a typed row whose worktree cwd is gone
but whose git repo still exists is kept: cc-resume rebuilds it. Every other row with a
missing cwd is skipped.
"""
import os
import re
import subprocess
import time

from . import PARKED_STATES, SUPERSEDED, procs, resumecmd
from . import tabs as tabfile

CLUSTER_ANCHOR_S = 300
CLUSTER_GAP_S = 30
BLIND = "hibernated (iTerm2 tabs unknown)"
TAB_OPEN = "hibernated (tab still open: focus it or `cc-sessions wake`)"
BATCH_SIZE = 15
RUN_TAB_DELAY_S = 3  # each MCP-heavy resume is ~460 MB: do not start them all at once
TYPED_TAB_DELAY_S = 0.4
BATCH_TIMEOUT_MARGIN_S = 120
# A session killed with its iTerm2 instance has ended by the time the next one is up; the
# margin covers an iTerm2 reopened while the last SessionEnd hooks still run.
INSTANCE_END_MARGIN_S = 60
STILL_RUNNING = "already running"
STILL_OPEN = "tab still open"
QUIET_SKIPS = (STILL_RUNNING, STILL_OPEN)


def rebuildable_worktree(cfg, cwd, isdir=os.path.isdir):
    """True when cwd is a worktree path (contains worktree_marker) whose git repo still
    exists, so cc-resume can rebuild a parked or removed worktree."""
    marker = cfg.get("worktree_marker")
    if not cwd or not marker or marker not in cwd:
        return False
    repo = cwd.split(marker, 1)[0]
    return bool(repo) and isdir(repo) and os.path.exists(os.path.join(repo, ".git"))


def max_age_s(cfg, auto=False, max_age_days=None):
    """Seconds; 0 (or less) days = no limit. --auto never looks back less than 30 days, and
    only an explicit --max-age-days 0 lifts its limit (a config 0 keeps the 30-day floor)."""
    explicit = max_age_days is not None
    days = float(max_age_days if explicit else cfg.get("restore_max_age_days", 7))
    if days <= 0 and (explicit or not auto):
        return float("inf")
    return max(days, 30) * 86400 if auto else days * 86400


def shutdown_cluster(rows, boot):
    """Session ids of the pre-boot shutdown cluster (possibly empty)."""
    pre = [r["last_event_at"] for r in rows if r.get("last_event_at") and r["last_event_at"] <= boot]
    others = sorted((r for r in rows if r.get("end_reason") == "other" and r.get("ended_at")
                     and r["ended_at"] <= boot), key=lambda r: -r["ended_at"])
    if not pre or not others:
        return set()
    t = others[0]["ended_at"]
    if max(pre) - t > CLUSTER_ANCHOR_S:
        return set()
    out, prev = set(), t
    for r in others:
        if prev - r["ended_at"] > CLUSTER_GAP_S:
            break
        out.add(r["session_id"])
        prev = r["ended_at"]
    return out


def select(rows, boot, now, cfg, alive_fn, auto=False, include_hibernated=False,
           live_guids=None, isdir=os.path.isdir, max_age_days=None):
    """-> (run, typed, skipped): run/typed are rows, skipped is [(row, reason)]."""
    cluster = shutdown_cluster(rows, boot) if boot else set()
    max_age = max_age_s(cfg, auto, max_age_days)
    run, typed, skipped = [], [], []
    for r in sorted(rows, key=lambda r: r.get("last_event_at") or 0):
        st = r.get("state")
        if st == SUPERSEDED:
            continue
        if r.get("source") not in ("hook", "legacy"):
            skipped.append((r, "source %s" % r.get("source")))
            continue
        if r.get("interactive") == 0:
            skipped.append((r, "non-interactive"))
            continue
        if alive_fn(r):
            skipped.append((r, "already running"))
            continue
        bucket = None
        if st in PARKED_STATES:
            if include_hibernated:
                if live_guids is not None and r.get("iterm_guid") in live_guids:
                    skipped.append((r, TAB_OPEN))
                    continue
                bucket = typed
            elif not auto:
                skipped.append((r, "hibernated"))
                continue
            elif live_guids is None:
                skipped.append((r, BLIND))
                continue
            elif r.get("restored_at") and boot and r["restored_at"] >= boot \
                    and r.get("iterm_guid") in live_guids:
                skipped.append((r, "hibernated (already retyped into a tab this boot)"))
                continue
            elif r.get("iterm_guid") and r["iterm_guid"] not in live_guids:
                bucket = typed
            else:
                skipped.append((r, "hibernated (tab still open)"))
                continue
        elif r.get("ended_at") is None:
            bucket = run
        elif r.get("end_reason") == "other" and r["session_id"] in cluster:
            bucket = run
        else:
            skipped.append((r, "exited (%s)" % r.get("end_reason")))
            continue
        last = r.get("last_event_at") or 0
        if auto and boot and last >= boot:
            skipped.append((r, "active after boot"))
            continue
        if now - last > max_age:
            skipped.append((r, "older than %dd" % (max_age / 86400)))
            continue
        cwd = r.get("cwd")
        rebuild = not auto and bucket is typed and rebuildable_worktree(cfg, cwd, isdir)
        if not cwd or not (isdir(cwd) or rebuild):
            skipped.append((r, "cwd gone: %s" % r.get("cwd")))
            continue
        bucket.append(r)
    return run, typed, skipped


def identity_unsure(r):
    """True when a ps failure or a missing start time means the row's process cannot be told
    from a dead one."""
    return procs.identity_probe(r.get("pid"), r.get("pid_start")) == procs.UNKNOWN


def select_snapshot(snap, rows, cfg, alive_fn, live_guids, isdir=os.path.isdir, until=None,
                    unsure_fn=None):
    """-> (run, typed, skipped, non_claude): the sessions of a snapshot's tabs in tab order,
    deduped by session, then the rows it could not have seen, newest first. run/typed are
    row copies (`tab_plan` merges them back into one order), skipped is [(row, reason)],
    non_claude the panes with no session. No age or count limit: the tab was open.
    `until` (tabs.next_instance_start) bounds the snapshot's instance: what ended after it
    was not killed with it. The cheap checks come first: `cc-sessions tabs` runs this for
    the menu every minute, and alive_fn may run ps."""
    live_guids = live_guids or ()
    taken_at = snap.get("taken_at") or 0
    run, typed, skipped, non_claude = [], [], [], []
    seen = set()

    def consider(r):
        ended = r.get("ended_at")
        restored = (r.get("restored_at") or 0) > taken_at
        # A live GUID alone proves nothing for a live-state row: stamp_visible rebinds any such
        # row on a tty to whichever tab holds it now, and a relaunched iTerm2's first empty tab
        # takes the lowest free pty, often a dead tab's. Only a parked row, a row restore bound
        # or a running process (the alive_fn branch below) really is in that tab.
        tab_live = bool(r.get("iterm_guid")) and r["iterm_guid"] in live_guids
        if r.get("state") == SUPERSEDED:
            why = "superseded"
        elif r.get("interactive") == 0:
            why = "non-interactive"
        elif tab_live and (restored or r.get("state") in PARKED_STATES):
            why = STILL_OPEN
        elif ended is not None and r.get("end_reason") not in (None, "other"):
            why = "exited (%s)" % r.get("end_reason")
        elif restored:
            why = "already restored; its tab was closed since"
        elif until and ended is not None and ended > until + INSTANCE_END_MARGIN_S:
            why = "ended after that iTerm2 instance"
        elif alive_fn(r):
            why = STILL_OPEN if tab_live else STILL_RUNNING
        elif tab_live and unsure_fn is not None and unsure_fn(r):
            # a second --resume of a running session forks it: leave it, and say so
            why = "process state unknown and its tab is open: not resumed"
        else:
            cwd = r.get("cwd")
            if not cwd or not (isdir(cwd) or rebuildable_worktree(cfg, cwd, isdir)):
                why = "cwd gone: %s" % cwd
            else:
                bucket = typed if r.get("state") in PARKED_STATES else run
                bucket.append(dict(r, _tab_order=len(run) + len(typed)))
                return
        skipped.append((r, why))

    by_id = {r["session_id"]: r for r in rows}
    for pane in tabfile.panes(snap):
        sid = pane.get("session_id")
        if not sid:
            non_claude.append(pane)
        elif sid not in seen:
            seen.add(sid)
            if sid in by_id:
                consider(by_id[sid])
            else:
                skipped.append(({"session_id": sid, "cwd": pane.get("cwd")}, "no ledger row"))

    def since(r):
        return max(r.get("started_at") or 0, r.get("resumed_at") or 0)
    unseen = [r for r in rows if r["session_id"] not in seen and r.get("state") != SUPERSEDED
              and r.get("interactive") != 0 and since(r) > taken_at
              and (not until or since(r) < until)
              and (r.get("ended_at") is None or r.get("end_reason") == "other")]
    for r in sorted(unseen, key=since, reverse=True):
        consider(r)
    return run, typed, skipped, non_claude


def tab_plan(run, typed):
    """[(row, run_it)] in tab order."""
    both = [(r, True) for r in run] + [(r, False) for r in typed]
    return sorted(both, key=lambda e: e[0]["_tab_order"])


def _as_string(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def batch_script(tabs, window_id=None):
    """AppleScript for one batch. tabs: [(command, run_it)]. Creates the window when
    window_id is None. Reports "window <id>", one "<session id> <tty>" per tab and, instead
    of failing, "error <text>": what opened before a failure must still get bound."""
    lines = ['set o to ""', 'tell application "iTerm2"', "activate"]
    first_is_window = window_id is None
    if first_is_window:
        lines.append("set w to (create window with default profile)")
    else:
        lines.append("set w to window id %d" % window_id)
    lines += ['set o to o & "window " & (id of w) & linefeed', "try"]
    for i, (cmd, run_it) in enumerate(tabs):
        if first_is_window and i == 0:
            lines.append("set s to current session of w")
        else:
            lines += ["tell w to set t to (create tab with default profile)",
                      "set s to current session of t"]
        lines += ['tell s to write text "%s"%s' % (_as_string(cmd), "" if run_it else " newline NO"),
                  "delay %s" % (RUN_TAB_DELAY_S if run_it else TYPED_TAB_DELAY_S),
                  'set y to ""',
                  "try",
                  "set y to tty of s",
                  'if y is missing value then set y to ""',
                  "end try",
                  'set o to o & (id of s) & " " & y & linefeed']
    lines += ["on error m", 'set o to o & "error " & m & linefeed', "end try", "end tell", "return o"]
    return "\n".join(lines) + "\n"


def parse_batch(stdout):
    """-> (window id or None, [(session id or None, tty or None)] per tab, error or None).
    A malformed session id keeps its position and is None: that tab opened, unbindable."""
    window, opened, error = None, [], None
    for line in (stdout or "").splitlines():
        if line.startswith("window "):
            try:
                window = int(line.split()[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("error "):
            error = line[6:]
        else:
            parts = line.split()
            if parts:
                tty = parts[1] if len(parts) == 2 and TTY_RE.match(parts[1]) else None
                opened.append((parts[0] if GUID_RE.match(parts[0]) else None, procs.norm_tty(tty)))
    return window, opened, error


def _osascript(script, timeout=None):
    r = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("osascript failed: %s" % (r.stderr or "").strip()[:200])
    return r.stdout


def _batch_timeout(batch):
    return BATCH_TIMEOUT_MARGIN_S + sum(RUN_TAB_DELAY_S if run_it else TYPED_TAB_DELAY_S
                                        for _, run_it in batch)


def open_tabs(tabs, progress):
    """Open tabs in one new window, BATCH_SIZE per osascript run, reusing the first run's
    window id. progress["window"] and progress["opened"] grow as batches return, so the
    caller still knows what exists when this raises."""
    for i in range(0, len(tabs), BATCH_SIZE):
        batch = tabs[i:i + BATCH_SIZE]
        window, opened, error = parse_batch(
            _osascript(batch_script(batch, progress.get("window")), _batch_timeout(batch)))
        if window is not None and progress.get("window") is None:
            progress["window"] = window
        progress["opened"].extend(opened)
        if error or len(opened) != len(batch):
            raise RuntimeError(error or "iTerm2 reported %d of %d tabs" % (len(opened), len(batch)))
        if progress.get("window") is None:
            raise RuntimeError("iTerm2 did not report a window id")


def select_first_tab(window):
    _osascript('tell application "iTerm2"\ntell window id %d\ntell first tab to select\n'
               "end tell\nend tell\n" % window, BATCH_TIMEOUT_MARGIN_S)


def bind_opened(conn, plan, opened, out=print):
    """mark_restored every opened tab, run rows too (a second click then sees them through
    live_guids), then ask the daemon to title the typed ones. -> rows bound. Only once all
    tabs exist: creating a tab selects it, and the daemon resumes a hibernated row whose
    GUID matches the selected tab."""
    from .ledger import add_request, mark_restored
    bound = 0
    for (row, run_it), (guid, tty) in zip(plan, opened):
        if not guid:
            continue
        sid = row["session_id"]
        try:
            n = mark_restored(conn, sid, guid, tty, seen=(row.get("state"), row.get("state_since")))
            bound += n
            if n and not run_it:
                add_request(conn, "title", sid)
        except Exception as exc:
            out("  could not bind %s: %s" % (sid[:8], exc))
    return bound


def run_snapshot(cfg, conn, auto=False, dry_run=False, snapshot=None, out=print):
    """The snapshot path of `restore`, holding the restore lock for the whole run. A
    dry run opens nothing and takes no lock, so a preview can never make a real run lose."""
    lock = None
    if not dry_run:
        lock = tabfile.restore_lock(cfg)
        if lock is None:
            out("restore already running")
            return 0
    try:
        return _run_snapshot(cfg, conn, auto, dry_run, snapshot, out)
    finally:
        if lock is not None:
            lock.close()


def live_guids_of(current):
    """The live tab GUIDs: empty when iTerm2 is not running (no tab can be open, and opening
    the first one launches it; this is the login after a reboot), None when a running iTerm2
    could not be asked."""
    return set() if current is None else iterm_guids()


def _run_snapshot(cfg, conn, auto, dry_run, snapshot, out):
    from .ledger import all_rows
    current = tabfile.iterm_instance()
    entries = tabfile.list_snapshots(cfg)
    if snapshot:
        entry = tabfile.load_entry(os.path.expanduser(snapshot))
        if entry is None:
            out("cannot read snapshot %s" % snapshot)
            return 1
    else:
        entry = tabfile.previous(cfg, current, entries)
        if entry is None:
            out("nothing to restore")
            return 0
    path, snap = entry["path"], entry["snap"]
    if auto and not dry_run and tabfile.consumed(path):
        out("auto: %s was already restored" % os.path.basename(path))
        out("nothing to restore")
        return 0
    guids = live_guids_of(current)
    if guids is None:
        # never open blind, and an --auto abort must not consume the snapshot
        out("iTerm2 could not be asked; retry")
        return 1
    alive = lambda r: procs.identity_alive(r.get("pid"), r.get("pid_start"))
    runs, typed, skipped, non_claude = select_snapshot(
        snap, all_rows(conn), cfg, alive, guids,
        until=tabfile.next_instance_start(cfg, snap, current, entries), unsure_fn=identity_unsure)
    for r, why in skipped:
        if why not in QUIET_SKIPS:
            out("  skip %s: %s" % (r["session_id"][:8], why))
    if non_claude:
        out("  %d pane(s) without a session are not reopened" % len(non_claude))
    # STILL_RUNNING = alive with no live tab: possibly a process still dying when the login
    # agent fired, so the snapshot stays unconsumed for it. Nothing retries by itself; the
    # next --auto (the next load of the agent) or a manual run from the menu picks it up.
    dying = sum(1 for _, why in skipped if why == STILL_RUNNING)
    consume = auto and not dry_run and not dying
    if auto and not dry_run and dying:
        out("  %d session(s) running without a tab: snapshot not marked restored" % dying)
    plan = tab_plan(runs, typed)
    if not plan:
        if consume:
            tabfile.mark_consumed(path)
        out("nothing to restore")
        return 0
    for r, run_it in plan:
        note = "  (worktree will be rebuilt)" if not os.path.isdir(r["cwd"]) else ""
        out("  %s %s  %s  %s%s" % ("open " if run_it else "typed", r["session_id"][:8], r["cwd"],
                                  (r.get("title") or "")[:50], note))
    from_time = tabfile.when(snap["taken_at"])
    if dry_run:
        out("dry-run: would open %d tabs (%d running, %d typed) from %s" % (
            len(plan), len(runs), len(typed), from_time))
        return 0
    progress = {"window": None, "opened": []}
    error = None
    try:
        open_tabs([(resumecmd.command(cfg, r["session_id"], r["cwd"]), run_it) for r, run_it in plan],
                  progress)
    except Exception as exc:
        error = str(exc)
    finally:
        if progress["window"] is not None:
            try:
                select_first_tab(progress["window"])
            except Exception as exc:
                out("  could not select the first tab: %s" % exc)
        bind_opened(conn, plan, progress["opened"], out)
    opened_plan = plan[:len(progress["opened"])]
    n_run = sum(1 for _, run_it in opened_plan if run_it)
    if error:
        out("  %d of %d tabs did not open: %s" % (len(plan) - len(opened_plan), len(plan), error))
    elif consume:
        tabfile.mark_consumed(path)
    out("opened %d tabs (%d running, %d typed) from %s" % (
        len(opened_plan), n_run, len(opened_plan) - n_run, from_time))
    return 0


def osascript_for(tabs):
    """tabs: [(command, run_it)] -> AppleScript opening one window, one tab each."""
    lines = ['set o to ""', 'tell application "iTerm2"', "activate",
             "set w to (create window with default profile)", "set first_done to false"]
    for cmd, run_it in tabs:
        esc = cmd.replace("\\", "\\\\").replace('"', '\\"')
        wt = 'write text "%s"%s' % (esc, "" if run_it else " newline NO")
        lines += ["if first_done then",
                  "tell w to set t to (create tab with default profile)",
                  "set s to current session of t",
                  "else",
                  "set s to current session of w",
                  "set first_done to true",
                  "end if",
                  "tell s to " + wt,
                  "delay 0.4",
                  'set y to ""',
                  "try",
                  "set y to tty of s",
                  'if y is missing value then set y to ""',
                  "end try",
                  'set o to o & (id of s) & " " & y & linefeed']
    lines += ["end tell", "return o"]
    return lines


GUID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{7,}$")
TTY_RE = re.compile(r"^(/dev/)?tty\w+$")


def new_tabs(stdout, n):
    """[(session id, tty or None)] for exactly n created tabs with distinct, well-formed
    ids, else None. Each line is "<id> <tty>"; the tty is empty when iTerm2 had none yet."""
    tabs = []
    for l in (stdout or "").splitlines():
        parts = l.split()
        if parts:
            ok = len(parts) == 2 and TTY_RE.match(parts[1])
            tabs.append((parts[0], procs.norm_tty(parts[1]) if ok else None))
    ids = [g for g, _ in tabs]
    if len(ids) != n or len(set(ids)) != n or not all(GUID_RE.match(i) for i in ids):
        return None
    return tabs


def iterm_guids():
    """Set of live iTerm2 session GUIDs, or None when iTerm2 cannot be asked."""
    # -a: macOS pgrep skips its own ancestors by default, and iTerm2 is an ancestor of
    # every shell in an iTerm2 tab
    if subprocess.run(["pgrep", "-axq", "iTerm2"]).returncode != 0:
        return None
    script = ('tell application "iTerm2"\nset o to ""\nrepeat with w in windows\n'
              'repeat with t in tabs of w\nrepeat with s in sessions of t\n'
              'set o to o & (id of s) & linefeed\nend repeat\nend repeat\nend repeat\n'
              'return o\nend tell')
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    except subprocess.SubprocessError:
        return None
    if r.returncode != 0:
        return None
    return {l.strip() for l in r.stdout.splitlines() if l.strip()}


def run(cfg, conn, auto=False, dry_run=False, limit=None, include_hibernated=False, out=print,
        max_age_days=None, snapshot=None):
    if snapshot or tabfile.list_snapshots(cfg):
        return run_snapshot(cfg, conn, auto=auto, dry_run=dry_run, snapshot=snapshot, out=out)
    from .ledger import all_rows
    limit = limit or int(cfg.get("restore_limit", 40))
    boot = procs.boot_time()
    mark = cfg.path("bootmark")
    if auto and not dry_run and boot:
        try:
            with open(mark) as fh:
                if fh.read().strip() == str(boot):
                    out("auto: already restored for this boot")
                    return 0
        except OSError:
            pass

    def write_mark():
        if auto and not dry_run and boot:
            os.makedirs(os.path.dirname(mark), exist_ok=True)
            with open(mark, "w") as fh:
                fh.write(str(boot))

    alive = lambda r: procs.identity_alive(r.get("pid"), r.get("pid_start"))
    guids = iterm_guids() if (auto or include_hibernated) else None
    if include_hibernated and not auto and guids is None:
        out("warning: iTerm2 tabs could not be listed; a hibernated session whose tab is "
            "still open gets a second tab")
    runs, typed, skipped = select(all_rows(conn), boot, time.time(), cfg, alive, auto=auto,
                                  include_hibernated=include_hibernated, live_guids=guids,
                                  max_age_days=max_age_days)
    blind = auto and any(why == BLIND for _, why in skipped)
    if blind:
        out("iTerm2 tabs could not be listed: hibernated sessions skipped; the boot marker "
            "is not written so the next run retries")
    for r, why in skipped:
        if why != "already running":
            out("  skip %s: %s" % (r["session_id"][:8], why))
    tabs = [(resumecmd.command(cfg, r["session_id"], r["cwd"]), True) for r in runs]
    tabs += [(resumecmd.command(cfg, r["session_id"], r["cwd"]), False) for r in typed]
    if not tabs:
        if not blind:
            write_mark()
        out("nothing to restore.")
        return 0
    if len(tabs) > limit:
        out("%d sessions exceeds --limit %d; raise it if intentional." % (len(tabs), limit))
        return 1
    def note(r):
        return "  (worktree will be rebuilt)" if not os.path.isdir(r["cwd"]) else ""
    for r in runs:
        out("  open  %s  %s  %s%s" % (r["session_id"][:8], r["cwd"], (r.get("title") or "")[:50], note(r)))
    for r in typed:
        out("  typed %s  %s  %s%s" % (r["session_id"][:8], r["cwd"], (r.get("title") or "")[:50], note(r)))
    if dry_run:
        out("dry-run: would open %d tabs." % len(tabs))
        return 0
    args = ["osascript"]
    for l in osascript_for(tabs):
        args += ["-e", l]
    r = subprocess.run(args, check=True, capture_output=True, text=True)
    opened = new_tabs(r.stdout, len(tabs))
    if opened is None:
        out("iTerm2 did not return one session id per new tab; typed rows stay retryable and "
            "the boot marker is not written")
        return 0
    from .ledger import mark_restored
    # the new tab's tty too: the row's old one belongs to some other tab by now
    for i, row in enumerate(typed):
        guid, tty = opened[len(runs) + i]
        mark_restored(conn, row["session_id"], guid, tty,
                      seen=(row.get("state"), row.get("state_since")))
    if not blind:
        write_mark()
    out("opened %d tabs." % len(tabs))
    return 0
