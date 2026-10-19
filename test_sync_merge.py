#!/usr/bin/env python3
"""Regression tests for the never-lose-a-source rule in sync_to_console.py.

    python test_sync_merge.py

Standard library only. The scenario that used to bite: a city whose console
registry is empty or partial must never lose the sources in its committed file.
"""

import copy
import unittest

from sync_to_console import manual_key, merge_config

FILE = {
    "city": "Brampton", "slug": "brampton", "timezone": "America/Toronto",
    "wix": {"site_id": "s", "base": "b", "social": "x", "practical": "y", "spiritual": "z"},
    "sources": [
        {"id": "lib", "name": "Library", "kind": "communico", "url": "https://lib/x"},
        {"id": "bia", "name": "BIA", "kind": "tribe", "url": "https://bia/x"},
        {"id": "city", "name": "City", "kind": "probe", "url": "https://city/x"},
    ],
    "manual_events": [
        {"source_id": "city", "title": "Parade", "start": "2026-11-15T17:00:00", "category": "social", "url": "u"},
        {"source_id": "bia", "title": "Market", "start": "2026-12-05T10:00:00", "category": "social", "url": "u"},
    ],
}


def ids(cfg):
    return [s["id"] for s in cfg["sources"]]


class MergeTests(unittest.TestCase):
    def test_console_with_one_source_does_not_wipe_the_file(self):
        """The original trap: one console source used to replace the whole list."""
        console = {"slug": "brampton", "sources": [
            {"id": "new-x1", "name": "New", "kind": "probe", "url": "https://new/x"}]}
        merged, st = merge_config(FILE, console)
        self.assertEqual(ids(merged), ["lib", "bia", "city", "new-x1"])
        self.assertEqual(st["file_only"], ["lib", "bia", "city"])
        self.assertEqual(len(merged["manual_events"]), 2)

    def test_empty_console_keeps_everything(self):
        merged, _ = merge_config(FILE, {"slug": "brampton", "sources": []})
        self.assertEqual(ids(merged), ids(FILE))
        self.assertEqual(merged["manual_events"], FILE["manual_events"])

    def test_old_console_without_tombstone_fields(self):
        """Before the console is redeployed its export has no removed_* keys."""
        console = {"sources": [dict(FILE["sources"][0], name="Library (edited)")]}
        merged, _ = merge_config(FILE, console)
        self.assertEqual(ids(merged), ["lib", "bia", "city"])
        self.assertEqual(merged["sources"][0]["name"], "Library (edited)")

    def test_console_edit_wins(self):
        console = {"sources": [dict(FILE["sources"][1], enabled=False, url="https://bia/new")]}
        merged, _ = merge_config(FILE, console)
        bia = next(s for s in merged["sources"] if s["id"] == "bia")
        self.assertFalse(bia["enabled"])
        self.assertEqual(bia["url"], "https://bia/new")

    def test_only_explicit_removal_drops_a_source_and_its_events(self):
        console = {"sources": [FILE["sources"][0], FILE["sources"][2]], "removed_sources": ["bia"]}
        merged, st = merge_config(FILE, console)
        self.assertEqual(ids(merged), ["lib", "city"])
        self.assertEqual(st["removed"], ["bia"])
        self.assertEqual([e["title"] for e in merged["manual_events"]], ["Parade"])

    def test_removed_manual_event_stays_removed(self):
        console = {"sources": FILE["sources"], "removed_manual_events": [
            {"source_id": "city", "title": "Parade", "start": "2026-11-15T17:00:00"}]}
        merged, _ = merge_config(FILE, console)
        self.assertEqual([e["title"] for e in merged["manual_events"]], ["Market"])

    def test_manual_events_are_unioned_without_duplicates(self):
        console = {"sources": FILE["sources"], "manual_events": [
            dict(FILE["manual_events"][0], start="2026-11-15T17:00:00"),
            {"source_id": "lib", "title": "Console only", "start": "2026-11-20T10:00:00", "category": "practical", "url": "u"}]}
        merged, _ = merge_config(FILE, console)
        self.assertEqual(sorted(e["title"] for e in merged["manual_events"]), ["Console only", "Market", "Parade"])

    def test_city_metadata_from_console_wins_but_blanks_do_not_erase(self):
        console = {"sources": [], "timezone": "America/Vancouver", "wix": {}}
        merged, _ = merge_config(FILE, console)
        self.assertEqual(merged["timezone"], "America/Vancouver")
        self.assertEqual(merged["wix"], FILE["wix"])
        self.assertNotIn("removed_sources", merged)

    def test_input_not_mutated(self):
        before = copy.deepcopy(FILE)
        merge_config(FILE, {"sources": [], "removed_sources": ["lib"]})
        self.assertEqual(FILE, before)

    def test_manual_key_matches_console(self):
        # Mirrors manualKey() in his-events-console/src/lib/source-config.ts
        self.assertEqual(manual_key({"source_id": "a", "title": " Parade ", "start": "2026-11-15T17:00:00"}),
                         "a|parade|2026-11-15T17:00")


if __name__ == "__main__":
    unittest.main()
