"""cc-sessions command line. The `hook` subcommand is the hot path: it runs on every
Claude Code hook event, so it imports only what it needs and never fails the hook."""
import json
import os
import re
import sys
import time

from . import config

REQUIRED_HOOK_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "Stop",
                        "SubagentStart", "SubagentStop", "Notification")
OPTIONAL_HOOK_EVENTS = ("StopFailure",)
SAFE_SID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def log_line(cfg, msg):
    try:
        path = cfg.path("log") if cfg else config.log_path()
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(path, "a") as fh:
            fh.write("%s [hook] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def marker_path(cfg, sid):
    if not sid or not SAFE_SID.match(sid):
        return None
    return os.path.join(config.db_dir(cfg), "hook-failed", sid)


def find_proc(start_pid=None):
    """The claude process that fired this hook: one `ps -axo` walk plus one per-pid ps."""
    from . import procs
    pid = procs.find_claude(start_pid or os.getppid(), procs.ps_tree())
    if not pid:
        return None
    info = procs.proc_info(pid)
    if not info:
        return None
    return {"pid": pid, "tty": info["tty"], "pid_start": info["pid_start"],
            "interactive": procs.is_interactive_command(info["command"])}


def hook_main(event, stdin=None, proc_finder=None, now=None):
    """Apply one hook event to the ledger. Always returns 0 and prints nothing."""
    cfg = sid = None
    try:
        cfg = config.load()
        raw = (stdin or sys.stdin.buffer).read(4 * 1024 * 1024)
        payload = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
        if not isinstance(payload, dict):
            raise ValueError("hook payload is not an object")
        sid = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
        if not sid:
            raise ValueError("hook payload has no session_id")
        from . import ledger
        proc = (proc_finder or find_proc)()
        conn = ledger.connect(cfg.path("db"))
        try:
            ledger.apply_event(conn, event, payload, proc, now,
                               projects=cfg.path("claude_projects"))
        finally:
            conn.close()
        m = marker_path(cfg, sid)
        if m and os.path.exists(m):
            try:
                os.unlink(m)
            except OSError:
                pass
    except BaseException as exc:  # noqa: B902 - a hook must never fail Claude Code
        log_line(cfg, "%s %s failed: %s: %s" % (event, (sid or "?")[:8], type(exc).__name__, exc))
        try:
            m = marker_path(cfg, sid) if cfg else None
            if m:
                os.makedirs(os.path.dirname(m), exist_ok=True)
                with open(m, "w") as fh:
                    fh.write("%s %s\n" % (time.time(), event))
        except Exception:
            pass
    return 0


# ---------------------------------------------------------------- read-side commands

def _open(cfg, readonly=False):
    from . import ledger
    return ledger.connect(cfg.path("db"), readonly=readonly)


def resolve_sid(conn, sid):
    rows = conn.execute("SELECT session_id FROM sessions WHERE session_id LIKE ?",
                        (sid + "%",)).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    return sid


def cmd_state(cfg, a):
    try:
        conn = _open(cfg, readonly=True)
        st = conn.execute("SELECT state FROM sessions WHERE session_id=?", (a.sid,)).fetchone()
    except Exception:
        st = None
    if not st:
        print("unknown")
        return 1
    print(st[0])
    return 0


def _age(now, t):
    if not t:
        return "-"
    s = max(0, now - t)
    if s < 120:
        return "%ds" % s
    if s < 7200:
        return "%dm" % (s / 60)
    if s < 172800:
        return "%dh" % (s / 3600)
    return "%dd" % (s / 86400)


def status_data(cfg, conn, now=None):
    from . import ledger, procs
    now = time.time() if now is None else now
    rows = ledger.all_rows(conn, "ended_at IS NULL AND state <> 'ended' ORDER BY last_event_at DESC")
    ev = ledger.evictions_today(conn, now)
    hb = ledger.get_meta(conn, "heartbeat")
    table = procs.ps_commands()
    return {
        "now": now,
        "dry_run": bool(cfg.get("dry_run")),
        "config_error": cfg.error,
        "heartbeat_age_s": (now - float(hb)) if hb else None,
        "idle_hibernate_min": cfg.get("idle_hibernate_min") or 0,
        "swap_high_pct": cfg.get("swap_high_pct"),
        "memory": {"free_pct": procs.free_pct(), "swap_used_mb": procs.swap_used_mb(),
                   "swap_pct": procs.swap_pct(),
                   "top": [{"app": k, "rss_mb": round(v)} for k, v in procs.top_apps(table)]},
        "sessions": [{
            "session_id": r["session_id"], "state": r["state"],
            "idle_s": (now - r["state_since"]) if r.get("state_since") else None,
            "subagents": r.get("subagents") or 0, "cwd": r.get("cwd"), "title": r.get("title"),
            "focus_age_s": (now - r["last_focus_at"]) if r.get("last_focus_at") else None,
            "evictions_today": ev.get(r["session_id"], 0), "source": r.get("source"),
            "tty": r.get("tty"),
        } for r in rows],
    }


def cmd_status(cfg, a):
    conn = _open(cfg)
    d = status_data(cfg, conn)
    if a.json:
        print(json.dumps(d, indent=1))
        return 0
    now = d["now"]
    hb = d["heartbeat_age_s"]
    ih = d["idle_hibernate_min"]
    print("daemon heartbeat: %s   dry_run: %s   idle hibernate: %s%s" % (
        "never" if hb is None else _age(now, now - hb) + " ago", "on" if d["dry_run"] else "off",
        ("after %sm" % ih) if ih else "off",
        ("   CONFIG ERROR: " + d["config_error"]) if d["config_error"] else ""))
    m = d["memory"]
    print("memory: free %s%%   swap used %s MB (%s%%, evict at %s%%)   top: %s" % (
        "?" if m["free_pct"] is None else m["free_pct"],
        "?" if m["swap_used_mb"] is None else int(m["swap_used_mb"]),
        "?" if m["swap_pct"] is None else m["swap_pct"], d["swap_high_pct"] or "off",
        ", ".join("%s %dMB" % (t["app"], t["rss_mb"]) for t in m["top"])))
    print("%-10s %-6s %-3s %-6s %-2s %-8s %-8s %s" % ("STATE", "FOR", "SUB", "FOCUS", "EV", "TTY", "SID", "CWD  TITLE"))
    for s in d["sessions"]:
        print("%-10s %-6s %-3d %-6s %-2d %-8s %-8s %s  %s" % (
            s["state"], _age(now, now - s["idle_s"]) if s["idle_s"] is not None else "-",
            s["subagents"], _age(now, now - s["focus_age_s"]) if s["focus_age_s"] is not None else "-",
            s["evictions_today"], s["tty"] or "-", s["session_id"][:8],
            tilde_path(s["cwd"] or ""), (s["title"] or "")[:40]))
    return 0


def tilde_path(p):
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home) else p


def _request(cfg, kind, sid):
    from . import ledger
    conn = _open(cfg)
    if sid:
        sid = resolve_sid(conn, sid)
        if not ledger.get(conn, sid):
            print("unknown session %s" % sid, file=sys.stderr)
            return 1
    rid = ledger.add_request(conn, kind, sid)
    print("queued %s request #%d%s" % (kind, rid, " for " + sid[:8] if sid else ""))
    if daemon_freshness(conn)[0] == "stale":
        print("warning: the daemon heartbeat is stale; nothing will act on this until it runs",
              file=sys.stderr)
    return 0


def cmd_hibernate(cfg, a):
    if not a.all_idle and not a.sid:
        print("usage: cc-sessions hibernate <sid> | --all-idle", file=sys.stderr)
        return 2
    return _request(cfg, "hibernate-all-idle" if a.all_idle else "hibernate", a.sid)


def cmd_wake(cfg, a):
    if not a.all and not a.sid:
        print("usage: cc-sessions wake <sid> | --all", file=sys.stderr)
        return 2
    return _request(cfg, "wake-all" if a.all else "wake", a.sid)


def cmd_pending(cfg, a):
    from . import pending
    try:
        print(json.dumps(pending.pending_json(a.transcript)))
    except Exception:
        print(json.dumps({"pending": ["unreadable"], "last_text": ""}))
    return 0


def cmd_holds(cfg, a):
    from . import holds
    try:
        code, why = holds.holds(a.path, cfg)
    except Exception as exc:
        code, why = holds.UNREADABLE, "error: %s" % exc
    if a.verbose:
        print(why)
    return code


def cmd_restore(cfg, a):
    from . import restore
    if a.dismiss:
        return restore.dismiss(cfg)
    conn = _open(cfg)
    return restore.run(cfg, conn, auto=a.auto, dry_run=a.dry_run, limit=a.limit,
                       include_hibernated=a.include_hibernated, max_age_days=a.max_age_days,
                       snapshot=a.snapshot, sleep=time.sleep, after_open=restore.spawn_recolor)


def tabs_data(cfg, conn, ask=True, idle_days=None):
    """What `cc-sessions tabs` reports. `pending` (what restore would open now) is computed
    for the newest previous snapshot only; pending_known is false when a running iTerm2
    could not be asked, which also keeps null out of what the menu has to decode. Never a
    cookie or the API: the live tab list is one AppleScript call, shared by both numbers, and
    ask=False skips it (AppleScript to iTerm2 is what raises the Automation prompt)."""
    from . import activity, ledger, procs, restore, tabgroups, tabs
    current = tabs.iterm_instance()
    asked = []

    def live_guids():
        if not asked:
            asked.append(restore.live_guids_of(current) if ask or current is None else None)
        return asked[0]
    entries = tabs.list_snapshots(cfg)
    prev = tabs.previous(cfg, current, entries)
    pending = None
    dismissed = prev is not None and tabs.flagged(prev["path"], tabs.DISMISSED_SUFFIX)
    if dismissed:
        pending = 0
    elif prev is not None:
        guids = live_guids()
        if guids is not None:
            alive = lambda r: procs.identity_alive(r.get("pid"), r.get("pid_start"))
            run, typed, _, _ = restore.select_snapshot(
                prev["snap"], ledger.all_rows(conn), cfg, alive, guids,
                until=tabs.next_instance_start(cfg, prev["snap"], current, entries),
                unsure_fn=restore.identity_unsure)
            pending = len(run) + len(typed)
    snapshots = []
    for e in entries:
        s = e["snap"]
        d = {"path": e["path"], "taken_at": s["taken_at"], "time": tabs.when(s["taken_at"]),
             "iterm_pid": s.get("iterm_pid"), "iterm_start": s.get("iterm_start"),
             "is_current": tabs.same_instance(s, current), "before_shrink": e["sibling"],
             "tabs": tabs.tab_count(s), "sessions": tabs.session_count(s)}
        if e is prev and pending is not None:
            d["pending"] = pending
        snapshots.append(d)
    out = {"restoring": tabs.restore_running(cfg), "pending": pending or 0,
           "pending_known": prev is None or pending is not None,
           "pending_dismissed": dismissed,
           "pending_path": prev["path"] if prev else "",
           "pending_time": tabs.when(prev["snap"]["taken_at"]) if prev else "",
           "snapshots": snapshots}
    if idle_days is not None:
        doc, err = tabgroups.read(cfg)  # close-idle closes nothing on an unreadable file
        idle = 0 if err else activity.idle_estimate(conn, doc, idle_days, time.time(), live_guids)
        out["idle_tabs"] = idle or 0
        out["idle_tabs_known"] = idle is not None
    return out


def cmd_tabs(cfg, a):
    d = tabs_data(cfg, _open(cfg), ask=not a.no_ask, idle_days=a.idle_days)
    if a.json:
        print(json.dumps(d, indent=1))
        return 0
    if d["restoring"]:
        print("a restore is running")
    print("%-16s %-4s %-8s %-9s %-7s %s" % ("TAKEN", "TABS", "SESSIONS", "INSTANCE", "PENDING", "PATH"))
    for s in d["snapshots"]:
        print("%-16s %-4d %-8d %-9s %-7s %s" % (
            s["time"], s["tabs"], s["sessions"], "current" if s["is_current"] else s["iterm_pid"],
            s.get("pending", "-"), tilde_path(s["path"])))
    if not d["snapshots"]:
        print("(no snapshots yet)")
    if not d["pending_known"]:
        print("iTerm2 could not be asked, so what restore would open is unknown")
    return 0


def cmd_import_legacy(cfg, a):
    from . import legacy
    conn = _open(cfg)
    stats = legacy.import_legacy(conn, cfg, manifest_dir=a.manifests, sessions_dir=a.registry)
    print(json.dumps(stats))
    return 0


def settings_hook_events(settings_path):
    with open(settings_path) as fh:
        s = json.load(fh)
    found = set()
    for ev, groups in (s.get("hooks") or {}).items():
        for g in groups or []:
            for h in (g or {}).get("hooks") or []:
                if "cc-sessions hook" in str((h or {}).get("command", "")):
                    found.add(ev)
    return found


DAEMON_FRESH_S = 120
RESTART_LOOP_WINDOW_S = 300
RESTART_LOOP_STARTS = 3
BLIND_WARN_S = 300


def _meta_float(conn, key):
    from . import ledger
    try:
        v = ledger.get_meta(conn, key)
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def daemon_freshness(conn, now=None):
    """-> (verdict, heartbeat_age_s, pass_started_at, liveness_age_s). verdict: "fresh" (a
    tick completed within DAEMON_FRESH_S), "busy" (no recent tick, but an eviction pass is
    in progress and liveness, written with pass_started_at and after each eviction, is
    that fresh and no older than the pass start), else "stale"."""
    now = time.time() if now is None else now
    hb, lv = _meta_float(conn, "heartbeat"), _meta_float(conn, "liveness")
    started = _meta_float(conn, "pass_started_at")
    hb_age = now - hb if hb is not None else None
    lv_age = now - lv if lv is not None else None
    if hb_age is not None and hb_age < DAEMON_FRESH_S:
        verdict = "fresh"
    elif started is not None and lv is not None and lv >= started and lv_age < DAEMON_FRESH_S:
        verdict = "busy"
    else:
        verdict = "stale"
    return verdict, hb_age, started, lv_age


def _pid_running(conn, alive):
    from . import ledger
    pid = ledger.get_meta(conn, "pid")
    try:
        running = bool(pid) and alive(int(pid))
    except (TypeError, ValueError):
        running = False
    return pid, running


def daemon_checks(conn, now=None, alive=None):
    """[(status, name, detail)] from the daemon's meta rows. `heartbeat` = last completed
    tick; `liveness` = the daemon process is running (written at startup, on every
    connect attempt and after each eviction); `pass_started_at` = an eviction pass is in
    progress (a stale heartbeat then reads as busy while liveness is fresh and the pid
    runs); `connected_at` = last good iTerm2 connect (which clears `last_error`);
    `last_error` / `last_exit_at` = why and when it last gave up."""
    from . import ledger, procs
    now = time.time() if now is None else now
    alive = alive or procs.pid_alive
    out = []
    hb, lv = _meta_float(conn, "heartbeat"), _meta_float(conn, "liveness")
    hb_age = now - hb if hb is not None else None
    err = ledger.get_meta(conn, "last_error")
    exit_at = _meta_float(conn, "last_exit_at")
    connected_at = _meta_float(conn, "connected_at")
    try:
        starts = [float(t) for t in json.loads(ledger.get_meta(conn, "recent_starts") or "[]")]
    except (TypeError, ValueError):
        starts = []
    tick = "never" if hb_age is None else "%ds ago" % hb_age
    if connected_at is not None and (exit_at is None or connected_at > exit_at):
        link = "connected since %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(connected_at))
    else:
        since = max([t for t in (hb, exit_at) if t is not None] or starts[-1:] or [lv or now])
        link = "disconnected since %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
    verdict, _, started, lv_age = daemon_freshness(conn, now)
    if verdict == "fresh":
        out.append(("PASS", "daemon heartbeat", "%ds ago" % hb_age))
    elif verdict == "busy" and _pid_running(conn, alive)[1]:
        out.append(("PASS", "daemon heartbeat", "daemon busy: evicting since %s (liveness %ds ago, "
                    "last tick %s)" % (time.strftime("%H:%M:%S", time.localtime(started)), lv_age, tick)))
    elif lv is not None and now - lv < DAEMON_FRESH_S:
        pid, running = _pid_running(conn, alive)
        out.append(("FAIL", "daemon heartbeat", "daemon pid %s %s, last tick %s, %s: %s" % (
            pid or "?", "alive" if running else "not running", tick, link, err or "no error recorded")))
    else:
        pid, running = _pid_running(conn, alive)
        if running:
            detail = "daemon pid %s alive but unresponsive: no liveness for %s, last tick %s, %s" % (
                pid, "ever" if lv is None else "%ds" % (now - lv), tick, link)
        else:
            detail = "daemon not running (pid %s), last tick %s" % (pid or "?", tick)
        if err:
            detail += "; last error: %s" % err
        out.append(("FAIL", "daemon heartbeat", detail))
    blind = _meta_float(conn, "blind_since")
    if blind is not None and now - blind > BLIND_WARN_S:
        out.append(("WARN", "iTerm2 snapshot", "every session's tty read has failed for %d min "
                    "(%s skipped): focus tracking is blind" % (
                        (now - blind) // 60, ledger.get_meta(conn, "skipped_sessions") or "?")))
    recent = [t for t in starts if now - t < RESTART_LOOP_WINDOW_S]
    if len(recent) >= RESTART_LOOP_STARTS:
        out.append(("WARN", "daemon restarts", "%d starts in the last %d min (restart loop?); last error: %s" % (
            len(recent), RESTART_LOOP_WINDOW_S // 60, err or "none recorded")))
    return out


def tab_snapshot_check(cfg):
    """(status, name, detail): the running iTerm2 has a snapshot file, and how new it is. The
    file only changes when the tab list does, so an old taken_at alone is not a problem."""
    from . import tabs
    entries = tabs.list_snapshots(cfg)
    current = tabs.iterm_instance()
    if current is None:
        newest = "newest %s" % tabs.when(entries[0]["snap"]["taken_at"]) if entries else "none yet"
        return ("PASS", "tab snapshots", "iTerm2 not running; %s" % newest)
    mine = [e for e in entries if not e["sibling"] and tabs.same_instance(e["snap"], current)]
    if mine:
        return ("PASS", "tab snapshots", "iTerm2 pid %s: %d tabs, last changed %s" % (
            current["pid"], tabs.tab_count(mine[0]["snap"]), tabs.when(mine[0]["snap"]["taken_at"])))
    return ("WARN", "tab snapshots", "no snapshot of the running iTerm2 (pid %s) in %s; the "
            "daemon writes one each tick" % (current["pid"], tabs.snapshots_dir(cfg)))


def doctor_checks(cfg):
    """[(status, name, detail)] with status PASS | WARN | FAIL."""
    import subprocess
    import tempfile
    from . import SCHEMA_VERSION, ledger, pending, procs
    out = []
    if cfg.error:
        out.append(("FAIL", "config", cfg.error + " (dry_run forced on)"))
    else:
        out.append(("PASS", "config", cfg.source))
    try:
        conn = ledger.connect(cfg.path("db"))
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        out.append(("PASS" if v == SCHEMA_VERSION else "FAIL", "ledger", "%s schema %s" % (cfg.path("db"), v)))
        out.extend(daemon_checks(conn))
    except Exception as exc:
        out.append(("FAIL", "ledger", str(exc)))
    try:
        evs = settings_hook_events(cfg.path("claude_settings"))
        missing = [e for e in REQUIRED_HOOK_EVENTS if e not in evs]
        expected = REQUIRED_HOOK_EVENTS + OPTIONAL_HOOK_EVENTS
        wired = [e for e in expected if e in evs]
        if missing:
            detail = "missing: " + ", ".join(missing)
        elif len(wired) == len(expected):
            detail = "all %d events wired" % len(expected)
        else:
            detail = "%d of %d events wired (all required)" % (len(wired), len(expected))
        out.append(("FAIL" if missing else "PASS", "hooks", detail))
        for e in OPTIONAL_HOOK_EVENTS:
            if e not in evs:
                out.append(("WARN", "hooks", "%s not wired (optional)" % e))
    except Exception as exc:
        out.append(("FAIL", "hooks", "cannot read %s: %s" % (cfg.path("claude_settings"), exc)))
    py = cfg.path("daemon_python")
    try:
        r = subprocess.run([py, "-c", "import iterm2"], capture_output=True, text=True, timeout=30)
        out.append(("PASS" if r.returncode == 0 else "FAIL", "daemon python", "%s imports iterm2" % py
                    if r.returncode == 0 else (r.stderr.strip().splitlines() or ["?"])[-1]))
    except Exception as exc:
        out.append(("FAIL", "daemon python", "%s: %s" % (py, exc)))
    plist = cfg.path("launchd_plist")
    out.append(("PASS" if os.path.isfile(plist) else "FAIL", "launchd agent", plist))
    fp = procs.free_pct()
    out.append(("PASS" if fp is not None else "FAIL", "memory_pressure",
                "free %s%%" % fp if fp is not None else "unparseable"))
    sp = procs.swap_pct()
    out.append(("PASS" if sp is not None else "WARN", "swap",
                "used %s%% (pressure at %s%%)" % (sp, cfg.get("swap_high_pct") or "off")
                if sp is not None else "vm.swapusage unreadable or no swap (swap trigger idle)"))
    ih = cfg.get("idle_hibernate_min") or 0
    out.append(("PASS", "idle hibernate", "after %sm idle, %s per tick" % (
        ih, cfg.get("idle_hibernate_per_tick")) if ih else "off"))
    try:
        out.append(tab_snapshot_check(cfg))
    except Exception as exc:
        out.append(("WARN", "tab snapshots", str(exc)))
    with tempfile.TemporaryDirectory() as td:
        fails = pending.self_test(td)
    out.append(("FAIL" if fails else "PASS", "transcript shapes", "; ".join(fails) or "self-test ok"))
    return out


def cmd_doctor(cfg, a):
    checks = doctor_checks(cfg)
    for st, name, detail in checks:
        print("%-4s %-18s %s" % (st, name, detail))
    return 1 if any(c[0] == "FAIL" for c in checks) else 0


def reap(cfg, dry_run=False, cmd_table=None, cwds=None, alive=None):
    """-> (removed_sockets, orphans). Never kills anything."""
    from . import procs
    alive = alive or procs.pid_alive
    removed = []
    sd = cfg.path("sock_dir")
    try:
        names = os.listdir(sd)
    except OSError:
        names = []
    for n in names:
        m = re.match(r"^(\d+)\.sock$", n)
        if m and not alive(int(m.group(1))):
            if not dry_run:
                try:
                    os.unlink(os.path.join(sd, n))
                except OSError:
                    continue
            removed.append(n)
    cmd_table = procs.ps_commands() if cmd_table is None else cmd_table
    cwds = procs.cwd_map() if cwds is None else cwds
    orphans = []
    for pid, (ppid, rss, cmd) in cmd_table.items():
        if ppid != 1 or pid not in cwds:
            continue
        c = cwds[pid]
        if not os.path.isdir(c):
            orphans.append((pid, c, cmd))
    return removed, orphans


def cmd_reap(cfg, a):
    removed, orphans = reap(cfg, dry_run=a.dry_run)
    print("%s %d dead sockets in %s" % ("would remove" if a.dry_run else "removed", len(removed), cfg.path("sock_dir")))
    for pid, c, cmd in orphans:
        print("  orphan pid %d  cwd gone: %s  %s" % (pid, tilde_path(c), cmd[:80]))
    return 0


def cmd_daemon(cfg, argv):
    from . import resumecmd
    py = cfg.path("daemon_python")
    script = resumecmd.daemon_script()
    if not os.access(py, os.X_OK):
        print("daemon python %s is not executable (config daemon_python)" % py, file=sys.stderr)
        return 1
    os.execv(py, [py, script] + list(argv))


def build_parser():
    import argparse
    ap = argparse.ArgumentParser(prog="cc-sessions", description="Claude Code session ledger + memory daemon")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("state", help="print a session's state word")
    p.add_argument("sid")
    p = sub.add_parser("status", help="sessions, memory, daemon health")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("hibernate", help="ask the daemon to hibernate a session")
    p.add_argument("sid", nargs="?")
    p.add_argument("--all-idle", action="store_true")
    p = sub.add_parser("wake", help="ask the daemon to resume a hibernated session")
    p.add_argument("sid", nargs="?")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("pending", help="background work a transcript still has in flight (JSON)")
    p.add_argument("transcript")
    p = sub.add_parser("holds", help="exit 0 if a session holds PATH, 1 if not, 2 if unknown")
    p.add_argument("path")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("restore", help="reopen sessions a crash or shutdown killed")
    p.add_argument("--auto", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--include-hibernated", action="store_true")
    p.add_argument("--max-age-days", type=int, metavar="N",
                   help="override restore_max_age_days (0 = no limit)")
    p.add_argument("--snapshot", metavar="PATH", help="restore this snapshot file (see `tabs`)")
    p.add_argument("--dismiss", action="store_true", help="wave off the newest previous snapshot")
    p = sub.add_parser("tabs", help="tab snapshots, and what restore would open now")
    p.add_argument("--json", action="store_true")
    p.add_argument("--idle-days", type=float, metavar="N",
                   help="also estimate how many tabs `cc-tabs close-idle --days N` would close")
    p.add_argument("--no-ask", action="store_true", help="send no AppleScript to iTerm2")
    p = sub.add_parser("import-legacy", help="one-time import of v1 manifests + live registry")
    p.add_argument("--manifests")
    p.add_argument("--registry")
    sub.add_parser("doctor", help="health checks")
    p = sub.add_parser("reap", help="remove dead-pid sockets, list orphaned processes")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("daemon", help="run the daemon in the foreground (--once --dry-run to inspect)")
    return ap


COMMANDS = {"state": cmd_state, "status": cmd_status, "hibernate": cmd_hibernate,
            "wake": cmd_wake, "pending": cmd_pending, "holds": cmd_holds,
            "restore": cmd_restore, "tabs": cmd_tabs, "import-legacy": cmd_import_legacy,
            "doctor": cmd_doctor, "reap": cmd_reap}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "hook":
        return hook_main(argv[1] if len(argv) > 1 else "unknown")
    cfg = config.load()
    if argv and argv[0] == "daemon":
        return cmd_daemon(cfg, argv[1:])
    a = build_parser().parse_args(argv)
    if not a.cmd:
        build_parser().print_help()
        return 2
    return COMMANDS[a.cmd](cfg, a)
