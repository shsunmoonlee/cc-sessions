"""Process, tty and memory facts (macOS `ps`, `memory_pressure`, `sysctl`, `lsof`).

Process identity is (pid, start time): a pid alone is reused by the OS, so every check
that asks "is this row's process still running" compares the stored `pid_start` too.
"""
import calendar
import os
import re
import subprocess
import time

# LC_ALL=C fixes the lstart format; TZ=UTC0 makes it timezone-independent, so the parsed
# epoch is the same whatever zone the caller (hook, daemon, launchd) runs in.
PS_ENV = dict(os.environ, LC_ALL="C", LANG="C", TZ="UTC0")
CLAUDE_COMM = "claude"
SHELL_JOBS = ("zsh", "-zsh", "bash", "-bash", "fish", "-fish", "sh", "-sh", "login")

ALIVE, GONE, UNKNOWN = "alive", "gone", "unknown"


class ProcsError(Exception):
    """ps could not be run or timed out: the answer is unknown, not 'absent'."""


def _run_rc(args, timeout=10):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=PS_ENV)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcsError(str(exc))
    return r.returncode, r.stdout


def _run(args, timeout=10):
    try:
        rc, out = _run_rc(args, timeout)
    except ProcsError:
        return None
    return out if rc == 0 else None


def norm_tty(tty):
    """'/dev/ttys004' | 'ttys004' | 's004' -> 'ttys004'; '??' / '' / None -> None."""
    if not tty:
        return None
    t = str(tty).strip()
    if t.startswith("/dev/"):
        t = t[5:]
    if not t or t.startswith("?") or t == "-":
        return None
    if re.match(r"^s\d+$", t):
        t = "tty" + t
    return t


def parse_foreground(out):
    """`ps -t TTY -o pid=,pgid=,tpgid=,comm=` -> the name of what holds the terminal: the
    foreground group's non-shell member if it has one (conservative), else its shell; None
    when nothing is in the foreground. Names are basenames ('-zsh', 'claude', 'node')."""
    fg = []
    for line in (out or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, pgid, tpgid = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if tpgid > 0 and pgid == tpgid:
            fg.append((pid, os.path.basename(parts[3].strip())))
    if not fg:
        return None
    others = [name for _, name in fg if name not in SHELL_JOBS]
    return others[0] if others else fg[0][1]


def foreground_job(tty):
    """What holds TTY's foreground, from ps (the kernel's view; iTerm2's jobName lags it
    after a process exits). None when ps fails or the tty has no foreground group."""
    t = norm_tty(tty)
    if not t:
        return None
    try:
        rc, out = _run_rc(["ps", "-t", t, "-o", "pid=,pgid=,tpgid=,comm="])
    except ProcsError:
        return None
    return parse_foreground(out) if rc == 0 else None


def parse_ps_tree(out):
    """Parse `ps -axo pid=,ppid=,comm=` into {pid: (ppid, comm)}."""
    tree = {}
    for line in (out or "").splitlines():
        p = line.split(None, 2)
        if len(p) < 3:
            continue
        try:
            tree[int(p[0])] = (int(p[1]), p[2].strip())
        except ValueError:
            continue
    return tree


def ps_tree():
    """One `ps -axo pid=,ppid=,comm=` call (~30 ms; asking for lstart for every process
    costs ~140 ms, so start times are read per pid instead)."""
    return parse_ps_tree(_run(["ps", "-axo", "pid=,ppid=,comm="]))


def is_claude_comm(comm):
    return os.path.basename((comm or "").strip()) == CLAUDE_COMM


def find_claude(start_pid, tree, max_steps=6):
    """Walk ppid from start_pid to the first process whose comm basename is exactly
    'claude'. Returns the pid or None."""
    pid, steps = start_pid, 0
    while pid and pid in tree and steps <= max_steps:
        ppid, comm = tree[pid]
        if is_claude_comm(comm):
            return pid
        pid, steps = ppid, steps + 1
    return None


def lstart_epoch(tokens):
    """Five `ps -o lstart=` tokens printed under TZ=UTC0 -> epoch seconds (int)."""
    try:
        return int(calendar.timegm(time.strptime(" ".join(tokens), "%a %b %d %H:%M:%S %Y")))
    except (ValueError, TypeError):
        return None


def parse_proc_info(out):
    """`ps -o lstart=,tty=,command= -p PID` (TZ=UTC0) -> {pid_start, tty, command} or None.
    pid_start is epoch seconds."""
    toks = (out or "").strip().split(None, 6)
    if len(toks) < 6:
        return None
    start = lstart_epoch(toks[:5])
    if start is None:
        return None
    return {"pid_start": start, "tty": norm_tty(toks[5]),
            "command": toks[6] if len(toks) > 6 else ""}


def proc_info(pid):
    return parse_proc_info(_run(["ps", "-o", "lstart=,tty=,command=", "-p", str(pid)]))


def parse_lstart_comm(out):
    toks = (out or "").strip().split(None, 5)
    if len(toks) < 6:
        return None
    start = lstart_epoch(toks[:5])
    return None if start is None else (start, toks[5])


def lstart_comm(pid):
    """(start epoch, comm) for a running pid; None when ps reports no such process.
    Raises ProcsError when ps itself failed."""
    rc, out = _run_rc(["ps", "-o", "lstart=,comm=", "-p", str(pid)])
    if rc != 0 and not out.strip():
        return None
    ent = parse_lstart_comm(out)
    if ent is None:
        raise ProcsError("unparseable ps output for pid %s" % pid)
    return ent


NON_INTERACTIVE_FLAGS = ("-p", "--print", "--sdk-url")


def is_interactive_command(cmd):
    """False for headless runs: -p/--print, --sdk-url, or stream-json input/output."""
    if not cmd:
        return True
    toks = cmd.split()[1:]
    for i, tok in enumerate(toks):
        name, _, val = tok.partition("=")
        if name in NON_INTERACTIVE_FLAGS:
            return False
        if name in ("--input-format", "--output-format"):
            v = val or (toks[i + 1] if i + 1 < len(toks) else "")
            if v == "stream-json":
                return False
    return True


def pid_exists(pid):
    """True / False / None (unknown). EPERM means it exists (another user's process)."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, OverflowError):
        return None
    return True


def pid_alive(pid):
    return bool(pid_exists(pid))


def identity_probe(pid, pid_start, lookup=None, exists=None, want_claude=True):
    """ALIVE | GONE | UNKNOWN for the exact process (pid, start epoch).
    GONE only when the pid is absent (ESRCH) or now belongs to another process; a ps
    failure or a race is UNKNOWN. A row without a start time cannot be identified: UNKNOWN."""
    if not pid:
        return GONE
    if pid_start is None:
        return UNKNOWN
    pid = int(pid)
    exists = exists or pid_exists
    e = exists(pid)
    if e is False:
        return GONE
    if e is None:
        return UNKNOWN
    try:
        ent = (lookup or lstart_comm)(pid)
    except ProcsError:
        return UNKNOWN
    if ent is None:
        e2 = exists(pid)
        return GONE if e2 is False else UNKNOWN
    start, comm = ent
    if want_claude and not is_claude_comm(comm):
        return GONE
    try:
        same = int(start) == int(pid_start)
    except (TypeError, ValueError):
        return UNKNOWN
    return ALIVE if same else GONE


def identity_alive(pid, pid_start, lookup=None, exists=None):
    """True only when pid runs, is claude, and started at pid_start. A missing start time
    is never alive-for-row (R1)."""
    if not pid or pid_start is None:
        return False
    if lookup is not None and exists is None:
        exists = lambda p: True  # noqa: E731 - test lookups stand in for the process table
    return identity_probe(pid, pid_start, lookup=lookup, exists=exists) == ALIVE


def child_identities(pid):
    """[(child pid, start epoch)] for the direct children of pid, read before a kill."""
    out = _run(["ps", "-axo", "pid=,ppid="])
    kids = []
    for line in (out or "").splitlines():
        p = line.split()
        if len(p) == 2 and p[1] == str(pid):
            try:
                ent = lstart_comm(int(p[0]))
            except (ProcsError, ValueError):
                continue
            if ent:
                kids.append((int(p[0]), ent[0]))
    return kids


def ps_commands():
    """{pid: (ppid, rss_kb, command)} for every process (one ps call)."""
    out = _run(["ps", "-axo", "pid=,ppid=,rss=,command="])
    table = {}
    for line in (out or "").splitlines():
        p = line.split(None, 3)
        if len(p) < 3:
            continue
        try:
            table[int(p[0])] = (int(p[1]), int(p[2]), p[3] if len(p) > 3 else "")
        except ValueError:
            continue
    return table


SHELL_SNAPSHOT = "/.claude/shell-snapshots/snapshot-"
HOOKS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "hooks") + os.sep


def is_hook_runner(cmd):
    """A Claude Code hook runner: `bash|sh|zsh ~/.claude/hooks/<script>`, or a plugin hook
    launched as `/bin/sh -c export PATH=... ${CLAUDE_PLUGIN_ROOT}...`."""
    toks = (cmd or "").split()
    if len(toks) >= 2 and os.path.basename(toks[0]) in ("bash", "sh", "zsh") \
            and toks[1].startswith(HOOKS_DIR):
        return True
    return cmd.startswith("/bin/sh -c export PATH=") and "CLAUDE_PLUGIN_ROOT" in cmd


def working_children(pid, table, helper_patterns):
    """Descendants of pid that count as work. A Bash-tool/Monitor shell (its command
    sources a shell snapshot) is always work, whatever helper_patterns say. A helper (MCP
    server, launcher, hook runner) and its whole subtree are ignored; anything else
    (caffeinate, a dev server) is work."""
    kids = {}
    for cpid, ent in table.items():
        kids.setdefault(ent[0], []).append(cpid)
    out, stack, seen = [], list(kids.get(pid, [])), set()
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        cmd = table[c][-1]
        if SHELL_SNAPSHOT in cmd:
            out.append("shell task")
            stack.extend(kids.get(c, []))
            continue
        if is_hook_runner(cmd) or any(h in cmd for h in helper_patterns):
            continue
        first = (cmd.split() or ["?"])[0]
        if "shell-snapshots" in cmd or os.path.basename(first).lstrip("-") in ("zsh", "bash", "sh", "fish"):
            out.append("shell task")
        else:
            out.append(os.path.basename(first))
        stack.extend(kids.get(c, []))
    return out


def tree_rss_mb(pid, table):
    """RSS of pid plus all descendants, MB."""
    kids = {}
    for cpid, ent in table.items():
        kids.setdefault(ent[0], []).append(cpid)
    total, stack, seen = 0, [pid], set()
    while stack:
        c = stack.pop()
        if c in seen or c not in table:
            continue
        seen.add(c)
        total += table[c][1]
        stack.extend(kids.get(c, []))
    return total / 1024.0


def app_name(cmd):
    m = re.search(r"/([^/]+)\.app/", cmd or "")
    if m:
        return m.group(1)
    first = (cmd or "?").split() or ["?"]
    return os.path.basename(first[0]) or "?"


def top_apps(table, n=5):
    by = {}
    for _, (ppid, rss, cmd) in table.items():
        k = app_name(cmd)
        by[k] = by.get(k, 0) + rss
    return sorted(((k, v / 1024.0) for k, v in by.items()), key=lambda x: -x[1])[:n]


def parse_memory_pressure(out):
    m = re.search(r"free percentage:\s*(\d+)%", out or "")
    return int(m.group(1)) if m else None


def free_pct():
    """System-wide memory free percentage, or None when it cannot be read."""
    return parse_memory_pressure(_run(["memory_pressure"], timeout=15))


def swap_used_mb():
    out = _run(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used\s*=\s*([\d.]+)M", out or "")
    return float(m.group(1)) if m else None


def parse_swapusage(out):
    """(total_mb, used_mb) from `sysctl vm.swapusage`, or None when it does not parse or
    there is no swap."""
    t = re.search(r"total\s*=\s*([\d.]+)M", out or "")
    u = re.search(r"used\s*=\s*([\d.]+)M", out or "")
    if not t or not u:
        return None
    try:
        total, used = float(t.group(1)), float(u.group(1))
    except ValueError:
        return None
    if total <= 0:
        return None
    return total, used


def swap_pct():
    """Swap used as a percentage of swap total, or None when it cannot be read."""
    tu = parse_swapusage(_run(["sysctl", "-n", "vm.swapusage"]))
    return None if tu is None else round(tu[1] * 100.0 / tu[0], 1)


def parse_boottime(out):
    m = re.search(r"sec\s*=\s*(\d+)", out or "")
    return int(m.group(1)) if m else None


def boot_time():
    return parse_boottime(_run(["sysctl", "-n", "kern.boottime"]))


def cwd_map():
    """{pid: cwd} for this user's processes (one lsof call); {} when unavailable."""
    out = _run(["lsof", "-n", "-a", "-d", "cwd", "-u", str(os.getuid()), "-Fpn"], timeout=25)
    m, pid = {}, None
    for line in (out or "").splitlines():
        if line[:1] == "p":
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line[:1] == "n" and pid:
            m[pid] = line[1:]
    return m
