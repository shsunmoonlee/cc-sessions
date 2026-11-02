# cc-sessions

Session bookkeeping for [Claude Code](https://code.claude.com) on macOS + iTerm2:

- a **ledger** of every session's state, written by Claude Code's own hooks (no scraping);
- a **daemon** that frees memory under pressure by hibernating the least recently used idle
  sessions (graceful SIGTERM, recorded first) and resumes one when you click its tab;
- **restore** after a crash, power loss or shutdown;
- **tab tools** for the tabs themselves: sort on demand, a session picker, closing old tabs on a
  click, manual groups and pins (see [Tabs](#tabs));
- **holds**, so a worktree cleaner can ask "does any session still need this directory?".

Formerly `cc-restore`. See [Migrating from v1](#migrating-from-v1).

## How it works

```
Claude Code hooks (8 events) --> cc-sessions hook <Event> --> ~/.claude/state/cc-sessions.db
                                                                  ^        |
launchd (KeepAlive) --> cc_sessions_daemon.py -------------------+        +--> restore, holds,
   iTerm2 API: focus events, tab control; memory_pressure; evict; resume      status, doctor
```

One writer per fact: hooks write session state (busy, idle, waiting, ended); the daemon
writes focus, evictions, requests and its heartbeat. Everything else only reads.

### Session states

| state | set by |
|---|---|
| `idle` | SessionStart, Stop, StopFailure (any error but `rate_limit`), Notification `idle_prompt` (when no subagents run and no usage-limit hold), `quota_auto_resume_stale` / `_disabled` |
| `busy` | UserPromptSubmit, Notification `quota_auto_resume_fired`; a StopFailure with `error: rate_limit` (the account's usage limit, an automatic resume pending) keeps the session busy for up to 26 h |
| `waiting` | Notification `permission_prompt` / `elicitation_dialog` |
| `ended` | SessionEnd |
| `evicting` -> `hibernated` | the daemon, then SessionEnd (or the daemon after the grace period) |
| `resuming` | cc-resume / the daemon, until SessionStart(resume) turns it `idle` |
| `superseded` | SessionStart in the same process under a new id (`/clear`, in-app `/resume`) |

A process is identified by (pid, start time), never by pid alone.

### When does the daemon evict?

Under memory pressure: when free memory (`memory_pressure`) drops below `low_free_pct` (20%); it stops at
`high_free_pct` (35%). If free memory does not rise by at least one point after an eviction,
it stops for `backoff_min` (10 min): the memory is somewhere else, and `cc-sessions status`
shows where.

Swap counts as pressure too: when swap used/total (`sysctl vm.swapusage`) is at or above
`swap_high_pct` (75%), the same pass runs even with free memory above `low_free_pct`, and
stops once swap is below `swap_high_pct` - 10 or free memory reaches `high_free_pct`. With
mostly swapped-out idle sessions, free memory barely moves on an eviction while swap does.
The log names the trigger (`trigger=free` or `trigger=swap`). An unreadable swap reading is
ignored.

Idle hibernation (off by default): with `idle_hibernate_min` above 0, every tick also
hibernates sessions whose state has been `idle` (not busy, waiting on a prompt or
resuming) for that many minutes, whatever the memory. Every guard below still applies,
with `idle_hibernate_min` in place of `idle_min` (idle time, last focus, file activity),
and the eviction is the same compare-and-set sequence. At most `idle_hibernate_per_tick`
(3) per tick, least recently focused first. This pass ignores the pressure backoff, never
overlaps a pressure pass, and logs `reason=idle <N>m`.

A session is a candidate only if every guard passes (each failure is logged as a keep
reason): written by hooks, interactive, idle for `idle_min` (10 min), no subagents, its
process identity still matches, its tty is an iTerm2 tab that is not the visible tab of any
window and was not focused in the last `idle_min`, not resumed in the last `cooldown_min`
(45 min), fewer than `max_evictions_per_day` (3) today, it has a transcript with a user
message, no transcript/subagent/task file changed in the last `idle_min`, no background
work in flight (async agents, workflows, background shells, monitors until their
timeout or a terminal notification, queued input, a scheduled wakeup, a non-durable cron
job, a usage-limit wait), and no child process other than MCP helpers and hook runners. Candidates go in
order of least recent focus, then largest memory.

"Visible" deliberately means every pane of the current tab of every iTerm2 window,
including minimized windows and windows on other Spaces: the daemon cannot tell whether
you are looking at a window, so it treats every window's front tab as seen. This is
conservative (such a tab is never evicted, and counts as seen on every tick).

Eviction: compare-and-set the row to `evicting`, re-check the guards, SIGTERM, SIGKILL only
after `term_grace_s` (15 s, logged as an INCIDENT). The tab gets a banner, the title
`[zz] <title>`, and the resume command typed but not run. Focusing that tab later runs it.
Whether a tab is at a shell comes from the tty's foreground process group (`ps`), and from
iTerm2's job name only when `ps` cannot tell: the job name lags, naming the exited claude
after the shell has the terminal back.

Ledger schema v3 only adds two `evictions` columns (`hook_at`, `hook_runs`). Code older than
v3 refuses a v3 ledger, so a rollback first runs
`sqlite3 ~/.claude/state/cc-sessions.db 'PRAGMA user_version=2'`.

## Install

Requirements: macOS, iTerm2 with the Python API enabled (Settings > General > Magic), the
system `/usr/bin/python3` (3.9+) for the CLI and hooks, and a python with the `iterm2`
package for the daemon:

```sh
python3 -m venv ~/.claude/venvs/iterm2 && ~/.claude/venvs/iterm2/bin/pip install iterm2
make install                       # PREFIX=~/.claude by default; nothing is started
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cc-sessions.daemon.plist
```

`make install` copies `bin/*` to `$PREFIX/bin`, the library to `$PREFIX/lib/cc-sessions/`,
and renders the launchd agent. `make check` exits 1 when an installed file differs from the
repo. `make uninstall` removes them.

Wire the hooks in `~/.claude/settings.json` (one entry per event):

```json
{ "hooks": {
  "SessionStart":     [{"hooks": [{"type": "command", "command": "cc-sessions hook SessionStart", "timeout": 5}]}],
  "SessionEnd":       [{"hooks": [{"type": "command", "command": "cc-sessions hook SessionEnd", "timeout": 5}]}],
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "cc-sessions hook UserPromptSubmit", "timeout": 5}]}],
  "Stop":             [{"hooks": [{"type": "command", "command": "cc-sessions hook Stop", "timeout": 5}]}],
  "StopFailure":      [{"hooks": [{"type": "command", "command": "cc-sessions hook StopFailure", "timeout": 5}]}],
  "SubagentStart":    [{"hooks": [{"type": "command", "command": "cc-sessions hook SubagentStart", "timeout": 5}]}],
  "SubagentStop":     [{"hooks": [{"type": "command", "command": "cc-sessions hook SubagentStop", "timeout": 5}]}],
  "Notification":     [{"hooks": [{"type": "command", "command": "cc-sessions hook Notification", "timeout": 5}]}]
} }
```

The hook always exits 0 and prints nothing; a failure is logged and leaves a marker that
blocks eviction of that session until its next successful hook.

Start with `"dry_run": true` in the config, watch `~/Library/Logs/cc-sessions.log`, then
turn it off. `cc-sessions doctor` checks the whole setup.

## Commands

```
cc-sessions status [--json]          sessions, idle ages, memory, top apps by RSS, heartbeat
cc-sessions state <sid>              the state word (exit 1 + "unknown" if not in the ledger)
cc-sessions hibernate <sid>|--all-idle   ask the daemon (explicit: skips pressure + idle time only)
cc-sessions wake <sid>|--all         resume in the original tab, or a new tab if it is gone
cc-sessions pending <transcript>     {"pending": [...], "last_text": "..."}; unreadable = pending
cc-sessions holds <path> [-v]        exit 0 held, 1 free, 2 ledger unreadable (treat != 1 as held)
cc-sessions restore [--auto] [--dry-run] [--dismiss] [--snapshot PATH] [--limit N] [--include-hibernated] [--max-age-days N]
cc-sessions tabs [--json]            tab snapshots, and what restore would open now
cc-sessions import-legacy [--manifests DIR] [--registry DIR]
cc-sessions doctor                   PASS/FAIL checks, exit 1 on any FAIL
cc-sessions reap [--dry-run]         delete dead-pid sockets; list (never kill) orphans
cc-sessions daemon [--once] [--dry-run]   run the daemon in the foreground; --once prints
                                          the candidate table with keep reasons and exits
cc-resume <sid> [--cwd DIR] [--dry-run]   resume a session, unparking its worktree if needed
cc-tabs sort [--all] [--dry-run]     order tabs, see below; cc-tabsort is the same command
cc-tabs find [--json]                every resumable session, newest first
cc-tabs open <sid>                   select its tab, or wake it in a new one
cc-tabs close-idle [--days 7] [--dry-run] [--json] [--only ID,ID]
cc-tabs group add <name> [--session ID] | remove [--session ID] | rename <old> <new>
cc-tabs group delete <name> | color <name> <#rrggbb|none> | list [--json]
cc-tabs pin [--session ID] [--toggle] | unpin [--session ID]
cc-tabs recolor                      reapply group colors to open tabs
```

### Tabs

Nothing moves or closes a tab unless you ask. No background sort, no automatic close, and no
automatic grouping: group names, members and colors are yours. Commands default to the tab
selected in the front iTerm2 window. `contrib/hammerspoon/cc_sessions_menu.lua` is the menu bar
item for all of this (`cc ⟲N` when N sessions are pending). `make install` copies it to
`~/.hammerspoon/` when that exists; require it in a pcall from `init.lua` and call `start()`. The
first use of the menu may ask for an Automation grant (Hammerspoon to iTerm2); until then its
refresh sends iTerm2 nothing, so a pending count shows only once iTerm2 is not running or the menu
has been opened. The refresh never connects to iTerm2's API.

**Restore.** After a Mac crash, a WindowServer crash or a restart you chose, the login agent
(`restore --auto`) reopens your tabs by itself. After iTerm2 went down alone (a clean quit, an
update, an iTerm2-only crash) they wait for a click: on losing iTerm2 the daemon waits
`iterm_alone_check_s`, and if `loginwindow` and `WindowServer` are the same processes as when the
snapshot was written, it marks the snapshot `.ended_alone`. A logout stops the daemon during that
wait, and a crash changes those processes, so neither is marked; anything it cannot read means no
marker, which means restoring. A marker written less than 10 minutes before the current boot is
ignored, since that was a restart whose quit phase outlasted the wait (another app's save sheet);
an unreadable boot time also means restoring. `--auto` waits up to 3 minutes for sessions still shutting down
and for an iTerm2 that does not answer yet, without holding up a click. `restore --dismiss` (menu:
`Dismiss`) waves off the pending snapshot; `--snapshot PATH` still opens it.

**Sort** (`cc-tabs sort`, menu or ⌃⌥⌘S): pinned; needs you (waiting on a prompt); working (anything
hibernation refuses to touch because work is in flight: busy, subagents, background shells and
monitors, a working child process); the rest, hibernated and idle mixed, where a group and an
ungrouped session rank alike by newest activity and a group's members sit together; other tabs
last, as they were. Activity is the newest prompt, turn end or finished subagent, else the
session's start or resume. A pinned, waiting or working tab stays in that section even when
grouped. It does not change the selected tab, so it wakes nothing. `--all` sorts every window.

**Find** (`Find session`, ⌃⌥⌘F): search by title, group or folder. Enter selects the tab, which
wakes a hibernated session, or wakes it in a new tab if its tab is gone.

**Close** (`Close about N tabs idle over 7 days`, always with a confirmation): the menu's number is
an estimate, the confirmation names an exact dry run, and the close takes at most those sessions,
refusing while a restore runs. A tab closes only when its
session is hibernated, its process is gone, it has been idle over `--days`, it is not pinned or in
a group, its foreground job is a shell, it is not a split tab and it is not the selected tab of a
window. An unreadable groups file closes nothing. The session stays hibernated, so Find and
`cc-sessions wake` still reach it. If a close drops more than half the tabs, the snapshot from
just before it is kept and `Older snapshots` undoes it; a smaller close keeps no such copy.

**Groups and pins** live in `cc-sessions-groups.json` beside the ledger (`groups_file`). A group has
no color until you give it one. Colors go on each session's own copy of its profile, so the shared
profile is untouched.

### restore

The daemon writes the ordered tab list to a JSON file on every tick where it changed, one
file per iTerm2 process lifetime, in `tab_snapshots_dir` (the newest 20 are kept). The last
write of a dead iTerm2 instance is the state at death, whatever killed it: power loss, kernel
panic, WindowServer, an iTerm2 crash or a clean quit. `restore` reopens the newest snapshot of
an instance that is no longer running, in one new window, in order:

- a tab that was running a session runs its resume command (one tab every 3 s, since each
  resume loads its MCP servers); a hibernated tab gets the command typed but not run, and its
  `[zz]` title, and resumes when you click it and press Enter (focus alone does not resume it
  for the first 60 s);
- a session you exited (`/exit`, Ctrl-D), one still running, or one whose tab is still open is
  skipped, so a second run opens nothing. A tab you closed before the crash is not in the
  file, and one you close after restoring it does not come back;
- sessions started after the last write are appended, newest first; a tab with no session is
  not reopened. There is no age or count limit: the tab was open. A missing cwd is reported,
  unless cc-resume can rebuild the worktree.

`--auto` consumes a snapshot at most once (a `.restored` marker beside it); manual runs ignore
the marker and rely on the filters above. `--snapshot PATH` picks another file, `--dry-run`
prints the plan. Only one restore runs at a time; a second prints `restore already running`.
If a running iTerm2 cannot be asked for its tabs, restore aborts rather than open blind. A
write that more than halves the tab count first keeps the old file as
`<name>.before-shrink-<time>.json`: a clean quit can tear tabs down between ticks, and
`tabs` lists such a file for `--snapshot`. `--limit`, `--include-hibernated` and
`--max-age-days` apply only to the legacy path below.

Window order in the file is iTerm2's creation order, not screen order.

On a machine with no snapshot yet, the legacy path restores sessions whose process is gone but
whose SessionEnd never arrived (crash, power loss), plus the shutdown cluster: sessions that ended with reason `other` together, right
before boot. A deliberate exit is never restored. `--auto` (for a login launchd job) runs at
most once per boot, only for sessions last active before boot, and also retypes (without
running) the resume command for hibernated sessions whose tab no longer exists.
`--max-age-days N` overrides `restore_max_age_days` (7; 0 = no limit). `--auto` never looks
back less than 30 days, and only an explicit `--max-age-days 0` lifts that floor. With
`--include-hibernated` (not `--auto`), a hibernated session whose worktree is gone but whose
git repo remains is still typed: cc-resume rebuilds the worktree.

### holds

For a worktree sweeper: a session holds `W` when its cwd (or launch cwd) is `W` or under
`W/` (a session in a parent directory does not), and it is alive, or hibernated within
`hibernate_keep_days` (60), or active within `session_age_days` (30), or a transcript for
`W` changed within `session_age_days`.

## Configuration

`~/.config/cc-sessions/config.json`, every key optional (`CC_SESSIONS_CONFIG` overrides the
path, `CC_SESSIONS_DB` the ledger, `CC_SESSIONS_LOG` the log). A config file that does not parse forces `dry_run` on.

| key | default | |
|---|---|---|
| `db` | `~/.claude/state/cc-sessions.db` | the ledger |
| `tab_snapshots_dir` | `cc-sessions-tabs` beside the ledger | tab snapshots (`~/.claude/state/cc-sessions-tabs` with the default ledger) |
| `iterm_alone_check_s` | 30 | after losing iTerm2, how long before deciding it went down alone |
| `groups_file` | `cc-sessions-groups.json` beside the ledger | manual groups and pins |
| `low_free_pct` / `high_free_pct` | 20 / 35 | evict below, stop at |
| `swap_high_pct` | 75 | swap used % that counts as memory pressure (0 = off) |
| `idle_min` | 10 | minutes idle, unfocused and without file activity |
| `idle_hibernate_min` | 0 (off) | hibernate any session idle this many minutes, regardless of memory |
| `idle_hibernate_per_tick` | 3 | most idle-pass evictions per tick |
| `cooldown_min` | 45 | no eviction this soon after a resume |
| `max_evictions_per_day` | 3 | per session |
| `max_evictions_per_pass` | 10 | most evictions one memory-pressure pass makes; the next tick continues (0 = no cap) |
| `term_grace_s` | 15 | SIGTERM to SIGKILL |
| `tick_s` | 30 | pressure check interval |
| `settle_s` / `backoff_min` | 10 / 10 | global backoff when an eviction frees nothing |
| `stale_busy_min` | 30 | busy this long + Esc-interrupted transcript = idle |
| `helper_patterns` | `["mcp", "npm exec ", "/.bin/", "-mcp"]` | child processes that are not work (hook runners always are not; a shell-snapshot shell always is) |
| `cron_recurring_block_h` | 24 | hours a non-durable recurring cron job keeps its session |
| `on_evict` | null | executable run after an eviction with `CC_SESSION_ID`, `CC_SESSION_CWD`, `CC_SESSION_TITLE`, `CC_EVICT_OUTCOME`; `CC_EVICT_OUTCOME` is `term`, `killed`, or for a replay `unknown`, `kill-failed` or `interrupted`. An eviction whose hook never ran (the daemon stopped anywhere between the signal and the hook) gets it on a later tick once the session is settled as hibernated. A hook is started at most twice per eviction, across daemon restarts, so it must be safe to run twice |
| `daemon_python` | `~/.claude/venvs/iterm2/bin/python` | python with `iterm2` |
| `resume_command` | installed `cc-resume` | what gets typed into tabs |
| `worktree_marker` | `/.claude/worktrees/` | worktree folder marker (cc-resume) |
| `park_archive_dir` | `~/.claude/repos/{repo_key}/archive` | where a sweeper archived a parked worktree |
| `park_branch_prefix` / `park_commit_subject` | `worktree/` / `wip(auto-park)` | how a parked worktree is recognized |
| `hibernate_keep_days` / `session_age_days` | 60 / 30 | holds windows |
| `log` | `~/Library/Logs/cc-sessions.log` | rotated 1 MB x 3 |
| `dry_run` | false | log "would evict" and never act |

## Development

```sh
make test            # /usr/bin/python3 -m unittest discover -s tests
make release-check   # CC_SESSIONS_PRIVATE_STRINGS=<file with one string per line>
```

Tests use temp ledgers and never signal processes or talk to iTerm2.

## Migrating from v1

v1 was five scripts (`cc-restore`, `cc-hibernate`, `cc-wake`, `cc-tabwatch`, `cc-daemon`)
that inferred session state from Claude Code's internal registry and transcripts, killed
idle sessions with SIGKILL on a timer, and kept hibernation manifests as JSON files.

1. `make install`, wire the hooks, `cc-sessions import-legacy` (imports un-woken
   hibernation manifests and live registry entries; idempotent).
2. Replace a `cc-restore --auto` launchd job with `cc-sessions restore --auto`.
3. Load the daemon agent in dry-run, then live.
4. Stop and remove `cc-daemon`, `cc-hibernate`, `cc-tabwatch`, `cc-wake`, `cc-restore` and
   any shell-profile line that started them.

Dropped: the transcript-mtime `--legacy` restore heuristic, and timer-based eviction.

## License

MIT
