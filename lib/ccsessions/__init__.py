"""cc-sessions: a hook-fed session ledger for Claude Code, plus a memory-pressure daemon.

Everything in this package runs on the system python3 (3.9, stdlib only), because the
hook path (`cc-sessions hook <Event>`) executes on every Claude Code event. The iTerm2
daemon (daemon/cc_sessions_daemon.py) imports this package from a python that has the
`iterm2` module.
"""

__version__ = "2.0.0"

SCHEMA_VERSION = 3

# Session states written to the ledger.
BUSY = "busy"
IDLE = "idle"
WAITING = "waiting"
EVICTING = "evicting"
HIBERNATED = "hibernated"
RESUMING = "resuming"
ENDED = "ended"
SUPERSEDED = "superseded"

LIVE_STATES = (BUSY, IDLE, WAITING)
PARKED_STATES = (HIBERNATED, EVICTING, RESUMING)
