"""Behaviour contracts for the capped local history store."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _loader import load

history = load("history")


def record(session_id: str, updated_at: float, **extra):
    return {
        "session_id": session_id,
        "model": "m",
        "provider": "p",
        "started_at": updated_at - 10,
        "updated_at": updated_at,
        "duration_ms": 1000,
        "ttft_ms": 300,
        "tps_core": 42.0,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_hit_pct": 70,
        "tool_time_ms": 250,
        "llm_calls": 3,
        **extra,
    }


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "history.json"

    def store(self, max_sessions=50):
        return history.HistoryStore(self.path, max_sessions)

    def test_upsert_and_load(self):
        store = self.store()
        self.assertTrue(store.upsert(record("a", 100)))
        rows = store.load()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "a")
        self.assertEqual(rows[0]["tps_core"], 42.0)

    def test_same_session_updates_in_place(self):
        store = self.store()
        store.upsert(record("a", 100))
        store.upsert(record("a", 200, llm_calls=9))
        rows = store.load()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["llm_calls"], 9)

    def test_cap_keeps_newest(self):
        store = self.store(max_sessions=3)
        for index in range(5):
            store.upsert(record(f"s{index}", 100 + index))
        rows = store.load()
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["session_id"] for row in rows], ["s4", "s3", "s2"])

    def test_delete_one_session(self):
        store = self.store()
        store.upsert(record("a", 100))
        store.upsert(record("b", 200))
        store.delete("a")
        self.assertEqual([row["session_id"] for row in store.load()], ["b"])

    def test_clear_keeps_the_file_valid(self):
        store = self.store()
        store.upsert(record("a", 100))
        store.clear()
        self.assertEqual(store.load(), [])
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["sessions"], [])

    def test_history_is_disabled_by_argument_not_by_luck(self):
        store = self.store(max_sessions=1)
        store.upsert(record("a", 100))
        store.upsert(record("b", 200))
        self.assertEqual(len(store.load()), 1)

    def test_missing_session_id_is_rejected(self):
        store = self.store()
        self.assertFalse(store.upsert({"model": "m"}))
        self.assertEqual(store.load(), [])

    def test_extra_fields_never_reach_disk(self):
        store = self.store()
        payload = record("a", 100)
        payload.update({"prompt": "secret", "response": "secret", "tool_args": {"x": 1}})
        store.upsert(payload)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("secret", raw)
        self.assertNotIn("tool_args", raw)
        for key in ("prompt", "response", "tool_args"):
            self.assertNotIn(key, store.load()[0])

    def test_corrupt_file_is_quarantined_not_fatal(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        store = self.store()
        self.assertEqual(store.load(), [])
        self.assertIn("unreadable", store.last_error)
        quarantined = list(self.path.parent.glob("history.json.corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        # A fresh write still works after the quarantine.
        self.assertTrue(store.upsert(record("a", 100)))
        self.assertEqual(len(store.load()), 1)

    def test_wrong_root_shape_is_quarantined(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"sessions": "nope"}), encoding="utf-8")
        store = self.store()
        self.assertEqual(store.load(), [])

    def test_stats_reports_size_and_cap(self):
        store = self.store(max_sessions=7)
        store.upsert(record("a", 100))
        stats = store.stats()
        self.assertEqual(stats["sessions"], 1)
        self.assertEqual(stats["max_sessions"], 7)
        self.assertGreater(stats["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
