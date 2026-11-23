"""tab_sort policy: quantized recency order (pure, no iTerm2)."""
import unittest

import helpers  # noqa: F401  (points the package at a temp ledger and config)

from ccsessions import IDLE, WAITING, tabgroups, tabsort


def row(sid, state=IDLE):
    return {"session_id": sid, "state": state}


class TestOrder(unittest.TestCase):
    def setUp(self):
        self.doc = tabgroups.empty()

    def order(self, tab_rows, events, bucket_s=600, work=None):
        return tabsort.order(tab_rows, events, self.doc, work or (lambda r: None), bucket_s)

    def test_recent_first_across_buckets(self):
        tabs = [[row("a")], [row("b")], [row("c")]]
        events = {"a": 1000.0, "b": 5000.0, "c": 3000.0}
        self.assertEqual(self.order(tabs, events, bucket_s=1), [1, 2, 0])

    def test_same_bucket_keeps_current_order(self):
        tabs = [[row("a")], [row("b")]]
        events = {"a": 700.0, "b": 1100.0}  # both bucket 1 at 600s
        self.assertEqual(self.order(tabs, events), [0, 1])

    def test_zero_bucket_sorts_raw(self):
        tabs = [[row("a")], [row("b")]]
        events = {"a": 700.0, "b": 1100.0}
        self.assertEqual(self.order(tabs, events, bucket_s=0), [1, 0])

    def test_pinned_then_needs_you_beat_recency(self):
        self.doc["sessions"]["c"] = {"group": None, "pinned": True, "at": 0}
        tabs = [[row("a")], [row("b", state=WAITING)], [row("c")]]
        events = {"a": 9999999.0, "b": 10.0, "c": 10.0}
        self.assertEqual(self.order(tabs, events), [2, 1, 0])

    def test_working_beats_a_newer_rest_tab(self):
        tabs = [[row("a")], [row("b")]]
        events = {"a": 9999999.0, "b": 10.0}
        out = self.order(tabs, events,
                         work=lambda r: "children" if r["session_id"] == "b" else None)
        self.assertEqual(out, [1, 0])

    def test_non_claude_tabs_trail_in_their_order(self):
        tabs = [[], [row("a")], [], [row("b")]]
        events = {"a": 700.0, "b": 999000.0}
        self.assertEqual(self.order(tabs, events), [3, 1, 0, 2])

    def test_split_tab_ranks_by_its_newest_pane(self):
        tabs = [[row("a")], [row("b"), row("c")]]
        events = {"a": 5000.0, "b": 10.0, "c": 9000.0}
        self.assertEqual(self.order(tabs, events, bucket_s=1), [1, 0])


if __name__ == "__main__":
    unittest.main()
