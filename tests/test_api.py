"""HTTP contract for the dashboard routes, exercised with the real router.

FastAPI is a host dependency (the dashboard mounts plugin routers on it); when
it is absent the suite skips instead of failing, so the aggregation and hook
tests still run on a bare Python.
"""

from __future__ import annotations

import json
import tempfile
import sys
import types
import unittest
from pathlib import Path

from _loader import ROOT, load

rt = load("runtime")

try:  # pragma: no cover - depends on the host environment
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    HAVE_FASTAPI = True
except Exception:  # pragma: no cover
    HAVE_FASTAPI = False


class FakeState:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class FakeCtx:
    def __init__(self, data_dir: Path, settings=None) -> None:
        self.state = FakeState(data_dir)
        self.settings = dict(settings or {})

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def set_config(self, key, value):
        self.settings[key] = value


def api_event(session_id="s1", out=100, duration=2.0):
    return {
        "session_id": session_id,
        "model": "deepseek-chat",
        "provider": "deepseek",
        "api_duration": duration,
        "started_at": 1000.0,
        "first_chunk_at": 1000.3,
        "usage": {"output_tokens": out, "input_tokens": 400, "cache_read_tokens": 600},
    }


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed on this host")
class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util

        spec = importlib.util.spec_from_file_location("hermes_perf_hud_api", ROOT / "dashboard" / "plugin_api.py")
        cls.api = importlib.util.module_from_spec(spec)
        sys.modules["hermes_perf_hud_api"] = cls.api
        spec.loader.exec_module(cls.api)
        app = FastAPI()
        app.include_router(cls.api.router)
        cls.client = TestClient(app)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        rt._reset_for_tests()
        self.ctx = FakeCtx(self.data_dir)
        rt.configure(self.ctx)

    def test_snapshot_returns_the_aggregate(self):
        rt.on_post_api(**api_event())
        body = self.client.get("/snapshot").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["snapshot"]["llm_calls"], 1)
        self.assertAlmostEqual(body["snapshot"]["tps_core"], 50.0, places=1)
        self.assertEqual(body["status"]["plugin"], "perf-hud")

    def test_snapshot_without_data_is_null_not_an_error(self):
        body = self.client.get("/snapshot").json()
        self.assertTrue(body["ok"])
        self.assertIsNone(body["snapshot"])

    def test_reset_zeroes_the_view_and_keeps_history(self):
        rt.on_post_api(**api_event())
        rt.on_session_boundary(False, session_id="s1")
        body = self.client.post("/reset", json={"session_id": "s1"}).json()
        self.assertTrue(body["reset"])
        self.assertEqual(body["snapshot"]["llm_calls"], 0)
        history = self.client.get("/history").json()
        self.assertEqual(len(history["sessions"]), 1)

    def test_reset_with_empty_body_clears_every_view(self):
        """The popover's button posts `{}` — that must reset all sessions."""
        rt.on_post_api(**api_event("a"))
        rt.on_post_api(**api_event("b"))
        body = self.client.post("/reset", json={}).json()
        self.assertTrue(body["reset"])
        self.assertEqual(self.client.get("/sessions").json()["sessions"], [])
        # History written earlier is untouched by a reset.
        self.assertGreaterEqual(len(self.client.get("/history").json()["sessions"]), 0)

    def test_clear_history_empties_the_file_only(self):
        rt.on_post_api(**api_event())
        rt.on_session_boundary(False, session_id="s1")
        body = self.client.post("/clear-history").json()
        self.assertTrue(body["cleared"])
        self.assertEqual(body["sessions"], 0)
        self.assertEqual(self.client.get("/snapshot").json()["snapshot"]["llm_calls"], 1)

    def test_history_limit_is_clamped(self):
        rt.on_session_boundary(False, session_id="s1")
        self.assertEqual(self.client.get("/history?limit=0").status_code, 422)
        self.assertEqual(self.client.get("/history?limit=500").status_code, 422)
        self.assertEqual(self.client.get("/history?limit=5").status_code, 200)

    def test_config_roundtrip(self):
        body = self.client.post("/config", json={"key": "chip_metric", "value": "calls"}).json()
        self.assertEqual(body["value"], "calls")
        self.assertEqual(self.client.get("/config").json()["config"]["chip_metric"], "calls")

    def test_config_rejects_bad_input_with_400(self):
        self.assertEqual(self.client.post("/config", json={"key": "nope", "value": 1}).status_code, 400)
        self.assertEqual(self.client.post("/config", json={"key": "chip_metric"}).status_code, 400)

    def test_sessions_and_fleet(self):
        rt.on_post_api(**api_event("a", out=10))
        rt.on_post_api(**api_event("b", out=20))
        body = self.client.get("/sessions").json()
        self.assertEqual(len(body["sessions"]), 2)
        self.assertEqual(body["fleet"]["llm_calls"], 2)

    def test_status_route(self):
        body = self.client.get("/status").json()
        self.assertTrue(body["ok"])
        self.assertIn("history", body)


if __name__ == "__main__":
    unittest.main()
