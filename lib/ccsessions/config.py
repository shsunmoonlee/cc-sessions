"""Configuration: ~/.config/cc-sessions/config.json, every key optional.

CC_SESSIONS_CONFIG overrides the config path, CC_SESSIONS_DB overrides the db path,
CC_SESSIONS_LOG overrides the log path (the test suite sets it so nothing it runs, a hook
subprocess included, can ever write to the real log).
A config file that exists but does not parse forces dry_run on (fail closed): a typo in
the file must never turn a dry-run daemon live.
"""
import json
import os

DEFAULT_CONFIG_PATH = "~/.config/cc-sessions/config.json"

DEFAULTS = {
    "db": "~/.claude/state/cc-sessions.db",
    "low_free_pct": 20,
    "high_free_pct": 35,
    # swap used/total at or above this counts as memory pressure (0 = off)
    "swap_high_pct": 75,
    # hibernate any session idle this many minutes, regardless of memory (0 = off)
    "idle_hibernate_min": 0,
    "idle_hibernate_per_tick": 3,
    "idle_min": 10,
    "cooldown_min": 45,
    "max_evictions_per_day": 3,
    # most evictions one memory-pressure pass makes before the tick ends (0 = no cap)
    "max_evictions_per_pass": 10,
    "term_grace_s": 15,
    "tick_s": 30,
    # Child processes that do not count as work (MCP servers and their launchers).
    # `caffeinate` is deliberately NOT here: Claude Code runs it while a turn is busy.
    # Hook runners (bash ~/.claude/hooks/*.sh, plugin `/bin/sh -c export PATH=` launchers)
    # are always helpers; a Bash-tool/Monitor shell (shell-snapshots) is always work.
    "helper_patterns": ["mcp", "npm exec ", "/.bin/", "-mcp"],
    # a non-durable recurring CronCreate job keeps the session this many hours after creation
    "cron_recurring_block_h": 24,
    "on_evict": None,
    "daemon_python": "~/.claude/venvs/iterm2/bin/python",
    "claude_projects": "~/.claude/projects",
    "claude_sessions": "~/.claude/sessions",
    "claude_settings": "~/.claude/settings.json",
    "claude_tmp": "/private/tmp/claude-{uid}",
    "log": "~/Library/Logs/cc-sessions.log",
    "sock_dir": "/tmp/cc-socks",
    "dry_run": False,
    "hibernate_keep_days": 60,
    "session_age_days": 30,
    "stale_busy_min": 30,
    "resume_timeout_s": 120,
    "settle_s": 10,
    "backoff_min": 10,
    "restore_limit": 40,
    "wake_all_limit": 10,
    "restore_max_age_days": 7,
    "bootmark": "~/.cache/cc-sessions.bootmark",
    # null = cc-sessions-tabs beside the ledger (~/.claude/state/cc-sessions-tabs by default)
    "tab_snapshots_dir": None,
    # after losing iTerm2, how long the daemon waits before deciding it went down alone: a
    # logout or restart stops launchd agents within this, so no marker gets written
    "iterm_alone_check_s": 30,
    # null = cc-sessions-groups.json beside the ledger
    "groups_file": None,
    # "recent": the daemon keeps every window's tabs in cc-tabs' sort order each tick
    # (pinned, needs you, working, the rest by newest activity); "off" = never by itself
    "tab_sort": "off",
    # tabs swap places by recency only across buckets this many seconds wide, so the
    # tab bar never dances on every hook event (0 = raw recency)
    "tab_sort_bucket_s": 600,
    # What gets typed into a tab to resume a session. null = the cc-resume installed
    # next to this package (bin/cc-resume).
    "resume_command": None,
    # Worktree parking (cc-resume). {repo_key} = the repo path with the leading / dropped
    # and every / replaced by -.
    "worktree_marker": "/.claude/worktrees/",
    "park_archive_dir": "~/.claude/repos/{repo_key}/archive",
    "park_branch_prefix": "worktree/",
    "park_commit_subject": "wip(auto-park)",
    # One-time import sources (import-legacy).
    "legacy_manifests": "~/.claude/state/hibernate",
    "launchd_plist": "~/Library/LaunchAgents/com.cc-sessions.daemon.plist",
}

PATH_KEYS = ("db", "daemon_python", "claude_projects", "claude_sessions", "claude_settings",
             "claude_tmp", "log", "sock_dir", "bootmark", "legacy_manifests", "launchd_plist",
             "on_evict", "tab_snapshots_dir", "groups_file")


def expand(p):
    if p is None:
        return None
    return os.path.expanduser(str(p).replace("{uid}", str(os.getuid())))


class Config(dict):
    error = None
    source = None

    def path(self, key):
        return expand(self.get(key))


def config_path():
    return expand(os.environ.get("CC_SESSIONS_CONFIG") or DEFAULT_CONFIG_PATH)


def load(path=None):
    cfg = Config(DEFAULTS)
    path = path or config_path()
    cfg.source = path
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("config root is not an object")
        cfg.update(data)
    except FileNotFoundError:
        pass
    except Exception as exc:  # unreadable or bad JSON: defaults, but never live
        cfg.error = "%s: %s" % (path, exc)
        cfg["dry_run"] = True
    if os.environ.get("CC_SESSIONS_DB"):
        cfg["db"] = os.environ["CC_SESSIONS_DB"]
    if os.environ.get("CC_SESSIONS_LOG"):
        cfg["log"] = os.environ["CC_SESSIONS_LOG"]
    return cfg


def log_path():
    """The log path when no config could be loaded (CC_SESSIONS_LOG, else the default)."""
    return expand(os.environ.get("CC_SESSIONS_LOG") or DEFAULTS["log"])


def db_dir(cfg):
    return os.path.dirname(cfg.path("db")) or "."
