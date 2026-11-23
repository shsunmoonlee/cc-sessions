"""Automatic tab order: the policy of the daemon's tab_sort pass.

The order is cc-tabs' (activity.tab_info / sort_order: pinned, needs you, working, then
the rest by newest activity with a group's members together, non-Claude tabs after), but
activity is quantized into tab_sort_bucket_s buckets first: a pass that runs every tick
must only move a tab when it crosses a bucket, never reshuffle the bar on every hook
event. Within a bucket the current order stands (both sorts are stable). Pure: callers
do the iTerm2 I/O."""
from . import activity, tabgroups


def quantized(infos, bucket_s):
    """tab_info dicts with activity floored to a bucket index; bucket_s <= 0 disables."""
    if bucket_s <= 0:
        return list(infos)
    out = []
    for i in infos:
        q = dict(i)
        q["activity"] = int((q.get("activity") or 0) // bucket_s)
        out.append(q)
    return out


def order(tab_rows, events, doc, work_fn, bucket_s):
    """Indexes of `tab_rows` (one list of ledger rows per tab, in current tab order) in
    the order the window should show. The identity order means nothing to move."""
    infos = [activity.tab_info(rows, events,
                               lambda s: tabgroups.group_of(doc, s),
                               lambda s: tabgroups.is_pinned(doc, s), work_fn)
             for rows in tab_rows]
    return activity.sort_order(quantized(infos, bucket_s))
