"""Manual tab groups and pins: {"groups": {name: {"color": "#rrggbb"|null}}, "sessions": {sid:
{"group": name|null, "pinned": bool, "at": ts}}}.

Names, membership and colors are the owner's: nothing here assigns a group or picks a color.
A missing or unreadable file reads as no groups, and an unreadable one is never overwritten:
a write raises until the owner has looked at it."""
import fcntl
import json
import os
import re
import time

from . import config
from . import tabs as tabfile

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class GroupsError(Exception):
    pass


def path(cfg):
    return cfg.path("groups_file") or os.path.join(config.db_dir(cfg), "cc-sessions-groups.json")


def empty():
    return {"groups": {}, "sessions": {}}


def _parse(raw):
    doc = json.loads(raw)
    if not isinstance(doc, dict) or not isinstance(doc.get("groups", {}), dict) \
            or not isinstance(doc.get("sessions", {}), dict):
        raise ValueError("not a groups file")
    doc.setdefault("groups", {})
    doc.setdefault("sessions", {})
    return doc


def load(cfg):
    """The document; empty when the file does not exist. Raises GroupsError when unreadable."""
    p = path(cfg)
    try:
        with open(p) as fh:
            return _parse(fh.read())
    except FileNotFoundError:
        return empty()
    except (OSError, ValueError) as exc:
        raise GroupsError("%s is unreadable: %s" % (p, exc))


def read(cfg):
    """-> (document, error or None). For readers that carry on with no groups."""
    try:
        return load(cfg), None
    except GroupsError as exc:
        return empty(), str(exc)


def _update(cfg, fn):
    """One locked read-change-write, so the menu and a shell cannot lose each other's edit."""
    p = path(cfg)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    with open(p + ".lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        doc = load(cfg)
        result = fn(doc)
        tabfile.atomic_write(p, json.dumps(doc, indent=1, sort_keys=True).encode("utf-8"))
    return result


def entry(doc, sid):
    e = doc["sessions"].get(sid)
    return e if isinstance(e, dict) else {}


def group_of(doc, sid):
    return entry(doc, sid).get("group")


def is_pinned(doc, sid):
    return bool(entry(doc, sid).get("pinned"))


def color_of(doc, sid):
    g = doc["groups"].get(group_of(doc, sid))
    return g.get("color") if isinstance(g, dict) else None


def members(doc, name):
    return sorted(s for s in doc["sessions"] if group_of(doc, s) == name)


def listing(doc):
    """Sorted groups. No color is "", not null: the menu decodes this."""
    return [{"name": n, "color": (doc["groups"][n] or {}).get("color") or "", "members": len(members(doc, n))}
            for n in sorted(doc["groups"])]


def _set(doc, sid, now, **fields):
    e = {"group": None, "pinned": False}
    e.update(entry(doc, sid))
    e.update(fields)
    e["at"] = time.time() if now is None else now
    doc["sessions"][sid] = e


def _clean_name(name):
    name = (name or "").strip()
    if not name or not name.isprintable():
        raise GroupsError("a group needs a printable name")
    return name


def _need(doc, name):
    if name not in doc["groups"]:
        raise GroupsError("no group named %s" % name)


def add(cfg, name, sid, now=None):
    """Put a session in a group, creating the group (with no color) when new."""
    name = _clean_name(name)

    def change(doc):
        doc["groups"].setdefault(name, {"color": None})
        _set(doc, sid, now, group=name)
    _update(cfg, change)


def remove(cfg, sid, now=None):
    _update(cfg, lambda doc: _set(doc, sid, now, group=None))


def rename(cfg, old, new, now=None):
    new = _clean_name(new)

    def change(doc):
        _need(doc, old)
        if new == old:
            return
        if new in doc["groups"]:
            raise GroupsError("a group named %s exists" % new)
        doc["groups"][new] = doc["groups"].pop(old)
        for s in members(doc, old):
            _set(doc, s, now, group=new)
    _update(cfg, change)


def delete(cfg, name, now=None):
    """Remove a group; its members become ungrouped. -> their session ids."""
    def change(doc):
        _need(doc, name)
        was = members(doc, name)
        for s in was:
            _set(doc, s, now, group=None)
        del doc["groups"][name]
        return was
    return _update(cfg, change)


def parse_color(value):
    """"#rrggbb" (lowercased) or "none" -> None. Raises GroupsError on anything else."""
    if str(value).lower() == "none":
        return None
    if not COLOR_RE.match(str(value)):
        raise GroupsError("a color is #rrggbb or none, not %s" % value)
    return str(value).lower()


def set_color(cfg, name, color):
    """-> the group's member session ids."""
    def change(doc):
        _need(doc, name)
        doc["groups"][name]["color"] = color
        return members(doc, name)
    return _update(cfg, change)


def set_pinned(cfg, sid, pinned, now=None):
    _update(cfg, lambda doc: _set(doc, sid, now, pinned=bool(pinned)))


def toggle_pinned(cfg, sid, now=None):
    """-> the new value."""
    def change(doc):
        new = not is_pinned(doc, sid)
        _set(doc, sid, now, pinned=new)
        return new
    return _update(cfg, change)


def rgb(color):
    """"#rrggbb" -> (r, g, b)."""
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
